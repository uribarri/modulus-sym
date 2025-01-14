# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Defines base class for all mesh type geometries
"""

import numpy as np
import csv
from stl import mesh as np_mesh
from sympy import Symbol

try:
    import pysdf.sdf as pysdf
except:
    print(
        "Error importing pysdf. Make sure 'libsdf.so' is in LD_LIBRARY_PATH and pysdf is installed"
    )
    raise

from .geometry import Geometry
from .parameterization import Parameterization, Bounds, Parameter
from .curve import Curve
from modulus.sym.constants import diff_str


class Tessellation(Geometry):
    """
    Constructive Tessellation Module that allows sampling on surface and interior
    of a tessellated geometry.

    Parameters
    ----------
    mesh : Mesh (numpy-stl)
        A mesh that defines the surface of the geometry.
    airtight : bool
        If the geometry is airtight or not. If false sample everywhere for interior.
    parameterization : Parameterization
        Parameterization of geometry.
    """

    def __init__(self, mesh, airtight=True, parameterization=Parameterization()):

        # make curves
        def _sample(mesh):
            def sample(
                nr_points, parameterization=Parameterization(), quasirandom=False
            ):
                # compute required points on per triangle
                triangle_areas = _area_of_triangles(mesh.v0, mesh.v1, mesh.v2)
                triangle_probabilities = triangle_areas / np.linalg.norm(
                    triangle_areas, ord=1
                )
                triangle_index = np.arange(triangle_probabilities.shape[0])
                points_per_triangle = np.random.choice(
                    triangle_index, nr_points, p=triangle_probabilities
                )
                points_per_triangle, _ = np.histogram(
                    points_per_triangle,
                    np.arange(triangle_probabilities.shape[0] + 1) - 0.5,
                )
                
                # isolate triangles with non-zero occupations
                nonzero_triangles = np.nonzero(points_per_triangle)[0]
                # compile list of indexes of triangles to be sampled
                triangle_select_indices = np.repeat(nonzero_triangles, points_per_triangle[nonzero_triangles])
                
                # go through every triangle and sample it
                invar = {}

                x, y, z = _sample_triangle(mesh.v0, mesh.v1, mesh.v2, triangle_select_indices)
                invar["x"] = x
                invar["y"] = y
                invar["z"] = z
                normal_scales = np.linalg.norm(mesh.normals[triangle_select_indices], axis = 1).reshape(-1,1)
                select_normals = mesh.normals[triangle_select_indices]/normal_scales
                invar["normal_x"] = select_normals[:,0].reshape(-1,1)
                invar["normal_y"] = select_normals[:,1].reshape(-1,1)
                invar["normal_z"] = select_normals[:,2].reshape(-1,1)
                invar["area"] = np.divide(triangle_areas,
                                        points_per_triangle,
                                        out=np.zeros_like(triangle_areas),
                                        where=points_per_triangle!=0
                                        )[triangle_select_indices].reshape((-1,1))

                # sample from the param ranges
                params = parameterization.sample(nr_points, quasirandom=quasirandom)
                return invar, params

            return sample

        curves = [Curve(_sample(mesh), dims=3, parameterization=parameterization)]

        # make sdf function
        def _sdf(triangles, airtight):
            def sdf(invar, params, compute_sdf_derivatives=False):
                # gather points
                points = np.stack([invar["x"], invar["y"], invar["z"]], axis=1)

                # normalize triangles and points
                minx, maxx, miny, maxy, minz, maxz = _find_mins_maxs(points)
                max_dis = max(max((maxx - minx), (maxy - miny)), (maxz - minz))
                store_triangles = np.array(triangles, dtype=np.float64)
                store_triangles[:, :, 0] -= minx
                store_triangles[:, :, 1] -= miny
                store_triangles[:, :, 2] -= minz
                store_triangles *= 1 / max_dis
                store_triangles = store_triangles.flatten()
                points[:, 0] -= minx
                points[:, 1] -= miny
                points[:, 2] -= minz
                points *= 1 / max_dis
                points = points.astype(np.float64).flatten()

                # compute sdf values
                outputs = {}
                if airtight:
                    sdf_field, sdf_derivative = pysdf.signed_distance_field(
                        store_triangles, points, include_hit_points=True
                    )
                    sdf_field = -np.expand_dims(max_dis * sdf_field, axis=1)
                else:
                    sdf_field = np.zeros_like(invar["x"])
                outputs["sdf"] = sdf_field

                # get sdf derivatives
                if compute_sdf_derivatives:
                    sdf_derivative = -(sdf_derivative - points)
                    sdf_derivative = np.reshape(
                        sdf_derivative, (sdf_derivative.shape[0] // 3, 3)
                    )
                    sdf_derivative = sdf_derivative / np.linalg.norm(
                        sdf_derivative, axis=1, keepdims=True
                    )
                    outputs["sdf" + diff_str + "x"] = sdf_derivative[:, 0:1]
                    outputs["sdf" + diff_str + "y"] = sdf_derivative[:, 1:2]
                    outputs["sdf" + diff_str + "z"] = sdf_derivative[:, 2:3]

                return outputs

            return sdf

        # compute bounds
        bounds = Bounds(
            {
                Parameter("x"): (
                    float(np.min(mesh.vectors[:, :, 0])),
                    float(np.max(mesh.vectors[:, :, 0])),
                ),
                Parameter("y"): (
                    float(np.min(mesh.vectors[:, :, 1])),
                    float(np.max(mesh.vectors[:, :, 1])),
                ),
                Parameter("z"): (
                    float(np.min(mesh.vectors[:, :, 2])),
                    float(np.max(mesh.vectors[:, :, 2])),
                ),
            },
            parameterization=parameterization,
        )

        # initialize geometry
        super(Tessellation, self).__init__(
            curves,
            _sdf(mesh.vectors, airtight),
            dims=3,
            bounds=bounds,
            parameterization=parameterization,
        )

    @classmethod
    def from_stl(
        cls,
        filename,
        airtight=True,
        parameterization=Parameterization(),
    ):
        """
        makes mesh from STL file

        Parameters
        ----------
        filename : str
          filename of mesh.
        airtight : bool
          If the geometry is airtight or not. If false sample everywhere for interior.
        parameterization : Parameterization
            Parameterization of geometry.
        """
        # read in mesh
        mesh = np_mesh.Mesh.from_file(filename)
        return cls(mesh, airtight, parameterization)

    @classmethod
    def from_multi_stl(
        cls,
        filename,
        airtight=True,
        parameterization=Parameterization(),
    ):
        """
        makes mesh from multi-STL file

        Parameters
        ----------
        filename : str
          filename of mesh.
        airtight : bool
          If the geometry is airtight or not. If false sample everywhere for interior.
        parameterization : Parameterization
            Parameterization of geometry.
        """
        # read in mesh
        mesh_gen = np_mesh.Mesh.from_multi_file(filename)
        full_mesh = np_mesh.Mesh(
            np.concatenate(
                [x.data for x in mesh_gen]
            )
        )
        return cls(full_mesh, airtight, parameterization)


# helper for sampling triangle
def _sample_triangle(
    v0, v1, v2, nzi
):  # ref https://math.stackexchange.com/questions/18686/uniform-random-point-in-triangle
    # v0,v1,v2 are complete vertex vectors from the mesh;
    # nzi is the array of indices of triangles to be sampled
    v0, v1, v2 = v0[nzi], v1[nzi], v2[nzi]
    n_triangle = v0.shape[0]
    np.random.seed(0)
    r1 = np.random.uniform(0, 1, size=(n_triangle, 1))
    r2 = np.random.uniform(0, 1, size=(n_triangle, 1))
    s1 = np.sqrt(r1)
    alpha = (1.0 - s1)
    beta = (1.0 - r2)*s1
    gamma = r2 * s1
    X = v0 * alpha + v1 * beta + v2 * gamma
    return X[:,0].reshape(-1,1), X[:,1].reshape(-1,1), X[:,2].reshape(-1,1)


# area of array of triangles
def _area_of_triangles(
    v0, v1, v2
):  # ref https://math.stackexchange.com/questions/128991/how-to-calculate-the-area-of-a-3d-triangle
    a = np.linalg.norm(v0-v1, axis = 1)
    b = np.linalg.norm(v1-v2, axis = 1)
    c = np.linalg.norm(v0-v2, axis = 1)
    s = (a + b + c) / 2
    area = np.sqrt(s * (s - a) * (s - b) * (s - c))
    return area


# helper for min max
def _find_mins_maxs(points):
    minx = float(np.min(points[:, 0]))
    miny = float(np.min(points[:, 1]))
    minz = float(np.min(points[:, 2]))
    maxx = float(np.max(points[:, 0]))
    maxy = float(np.max(points[:, 1]))
    maxz = float(np.max(points[:, 2]))
    return minx, maxx, miny, maxy, minz, maxz
