# Copyright (C) 2023, Princeton University.
# This source code is licensed under the BSD 3-Clause license found in the LICENSE file in the root directory of this source tree.

# Authors: Alexander Raistrick

import logging
import re

import bpy
import gin
import mathutils
import numpy as np
from tqdm import tqdm

from infinigen.core import surface
from infinigen.core.nodes.node_wrangler import (
    Nodes,
    NodeWrangler,
    geometry_node_group_empty_new,
)
from infinigen.core.placement import detail, split_in_view
from infinigen.core.util import blender as butil

from .factory import AssetFactory

logger = logging.getLogger(__name__)


def objects_to_grid(objects, spacing):
    rowsize = np.round(np.sqrt(len(objects)))
    for i, o in enumerate(objects):
        o.location += spacing * mathutils.Vector((i % rowsize, i // rowsize, 0))


def placeholder_locs(
    terrain, overall_density, selection, distance_min=0, altitude=0.0, max_locs=None
):
    temp_vert = butil.spawn_vert("compute_placeholder_locations")
    geo = temp_vert.modifiers.new(name="GEOMETRY", type="NODES")
    if geo.node_group is None:
        group = geometry_node_group_empty_new()
        geo.node_group = group
    nw = NodeWrangler(geo)

    base_geo = nw.new_node(Nodes.ObjectInfo, [terrain]).outputs["Geometry"]

    points = nw.new_node(
        Nodes.DistributePointsOnFaces,
        attrs={"distribute_method": "POISSON"},
        input_kwargs={
            "Mesh": base_geo,
            "Selection": surface.eval_argument(nw, selection),
            "Seed": np.random.randint(1e5),
            "Density Max": overall_density,
            "Distance Min": distance_min,
        },
    )
    verts = nw.new_node(Nodes.PointsToVertices, input_kwargs={"Points": points})
    verts = nw.new_node(
        Nodes.SetPosition, input_kwargs={"Geometry": verts, "Offset": (0, 0, altitude)}
    )

    nw.new_node(Nodes.GroupOutput, input_kwargs={"Geometry": verts})

    # dump the point locations out as vertices
    butil.apply_modifiers(temp_vert, geo)
    locations = np.array(
        [temp_vert.matrix_world @ v.co for v in temp_vert.data.vertices]
    )

    butil.delete(temp_vert)

    np.random.shuffle(locations)

    return locations


def points_near_camera(cam, scene_bvh, n, alt, dist_range):
    points = []

    while len(points) < n:
        rad = np.random.uniform(*dist_range)
        angle = np.deg2rad(np.random.uniform(0, 360))
        off = rad * mathutils.Vector((np.cos(angle), np.sin(angle), 0))
        pos = cam.location + off

        pos, *_ = scene_bvh.ray_cast(pos, mathutils.Vector((0, 0, -1)))
        if pos is None:
            continue
        pos.z += alt
        points.append(pos)

    return np.array(points)


def scatter_placeholders_mesh(
    base_mesh,
    factory: AssetFactory,
    overall_density,
    selection=None,
    distance_min=0,
    num_placeholders=None,
    **kwargs,
):
    locations = placeholder_locs(
        base_mesh, overall_density, selection, distance_min=distance_min, **kwargs
    )
    if num_placeholders is not None:
        np.random.shuffle(locations)
        if len(locations) < num_placeholders:
            area = butil.surface_area(base_mesh)
            logger.warning(
                f"Only returning {len(locations)} despite {num_placeholders=} requested. {base_mesh.name} had {area=} {overall_density=}"
            )
        locations = locations[:num_placeholders]
    return scatter_placeholders(locations, factory)


def scatter_placeholders(locations, factory: AssetFactory):
    logger.info(f"Placing {len(locations)} placeholders for {factory}")
    objs = []
    for i, loc in enumerate(tqdm(locations)):
        rot_z = np.random.uniform(0, 2 * np.pi)
        obj = factory.spawn_placeholder(i, loc, mathutils.Euler((0, 0, rot_z)))
        objs.append(obj)
    col = butil.group_in_collection(objs, "placeholders:" + repr(factory))
    factory.finalize_placeholders(objs)
    return col


def get_placeholder_points(obj: bpy.types.Object) -> np.ndarray:
    if obj.type == "MESH":
        verts = np.zeros((len(obj.data.vertices), 3))
        obj.data.vertices.foreach_get("co", verts.reshape(-1))
        return butil.apply_matrix_world(obj, verts)
    elif obj.type == "EMPTY" and obj.empty_display_type == "CUBE":
        extent = obj.empty_display_size * np.array([-1, 1])
        verts = np.stack(np.meshgrid(extent, extent, extent), axis=-1)
        return butil.apply_matrix_world(obj, verts)
    else:
        return np.array([obj.matrix_world.translation]).reshape(1, 3)


def parse_asset_name(name):
    match = re.fullmatch("(.*)\((\d+)\)\..*_(.*)\((\d+)\)", name)
    if not match:
        return None, None, None, None
    return list(match.groups())


def filter_populate_targets(
    placeholders: list[bpy.types.Object],
    cameras: list[bpy.types.Object],
    dist_cull: float,
    vis_cull: float,
    verbose: bool,
) -> list[tuple[bpy.types.Object, float, float]]:
    if verbose:
        placeholders = tqdm(placeholders)

    results = []

    for i, p in enumerate(placeholders):
        classname, *_ = parse_asset_name(p.name)

        if classname is None:
            raise ValueError(f"Could not parse {p.name=}, got {classname=}")

        mask, min_dists, min_vis_dists = split_in_view.compute_inview_distances(
            get_placeholder_points(p),
            cameras,
            dist_max=dist_cull,
            vis_margin=vis_cull,
            verbose=False,
        )

        dist = min_dists.min()
        vis_dist = min_vis_dists.min()

        if not mask.any():
            logger.debug(
                f"{p.name=} culled, not in view of any camera. {dist=} {vis_dist=}"
            )
            continue

        results.append((p, dist, vis_dist))

    return results


def populate_collection(
    factory: AssetFactory,
    placeholder_col: bpy.types.Collection,
    cameras: list[bpy.types.Object] = None,
    asset_col_target=None,
    dist_cull=None,
    vis_cull=None,
    verbose=True,
    cache_system=None,
    **asset_kwargs,
):
    if asset_col_target is None:
        asset_col_target = butil.get_collection(f"unique_assets:{repr(factory)}")

    placeholders = [o for o in placeholder_col.objects if o.parent is None]

    if cameras is not None:
        logger.info(f"Checking visibility for {placeholder_col.name=}")
        targets = filter_populate_targets(
            placeholders, cameras, dist_cull, vis_cull, verbose
        )
    else:
        targets = [(p, detail.scatter_res_distance(), 0) for p in placeholders]

    print(
        f"Populating {len(targets)} placeholders for {factory=} out of {len(placeholders)} total"
    )

    all_objs = []
    updated_pholders = []

    if verbose:
        targets = tqdm(targets)

    for i, (p, dist, vis_dist) in enumerate(targets):
        classname, inst_seed, *_ = parse_asset_name(p.name)

        if cache_system:
            if (
                sum(cache_system.n_placed.values()) < cache_system.max_fire_assets
                and cache_system.n_placed[factory.__class__.__name__]
                < cache_system.max_per_kind
            ):
                i_list = cache_system.find_i_list(factory)
                ind = np.random.choice(len(i_list))
                i_chosen, full_sim_folder, sim_folder = i_list[ind]
                obj = factory.spawn_asset(
                    int(i_chosen), placeholder=p, distance=dist, vis_distance=vis_dist
                )
                cache_system.link_fire(full_sim_folder, sim_folder, obj, factory)
            else:
                break

            continue

        obj = factory.spawn_asset(
            i, placeholder=p, distance=dist, vis_distance=vis_dist, **asset_kwargs
        )

        if p is not obj:
            p.hide_render = True

        for o in butil.iter_object_tree(obj):
            butil.put_in_collection(o, asset_col_target)

        obj["dist"] = dist
        obj["vis_dist"] = vis_dist

        updated_pholders.append((inst_seed, p))
        all_objs.append((inst_seed, obj))

    asset_col_target.hide_viewport = False
    factory.finalize_assets([r for i, r in all_objs])
    asset_col_target.hide_viewport = True

    return all_objs, updated_pholders


@gin.configurable
def populate_all(
    factory_class: type,
    cameras: list[bpy.types.Object],
    dist_cull=200,
    vis_cull=0,
    cache_system=None,
    **kwargs,
):
    """
    Find all collections that may have been produced by factory_class, and update them

    dist_cull: the max dist away from the camera to still populate assets
    vis_cull: the max dist outside of the view frustrum to still populate assets

    """

    results = []
    for col in bpy.data.collections:
        if not (match := re.fullmatch("placeholders:((.*)\((\d*)\))", col.name)):
            continue
        full_repr, classname, fac_seed = match.groups()

        if classname != factory_class.__name__:
            continue

        asset_target_col = butil.get_collection(f"unique_assets:{full_repr}")
        asset_target_col.hide_viewport = False

        if len(asset_target_col.objects) > 0:
            logger.info(
                f"Skipping populating {col.name=} since {asset_target_col.name=} is already populated"
            )
            continue

        fac_inst = factory_class(int(fac_seed), **kwargs)

        new_assets, pholders = populate_collection(
            fac_inst,
            placeholder_col=col,
            cameras=cameras,
            asset_target_col=asset_target_col,
            dist_cull=dist_cull,
            vis_cull=vis_cull,
            cache_system=cache_system,
        )
        results.append((fac_seed, pholders, new_assets))

    return results


def make_placeholders_float(placeholder_col, scene_bvh, water):
    deps = bpy.context.evaluated_depsgraph_get()
    water_bvh = mathutils.bvhtree.BVHTree.FromObject(water, deps)
    up = mathutils.Vector((0, 0, 1))
    margin = mathutils.Vector((0, 0, 1e-3))

    for p in tqdm(
        placeholder_col.objects,
        desc=f"Computing fluid-floating locations for {placeholder_col.name=}",
    ):
        w_up, *_ = water_bvh.ray_cast(p.location + margin, up)
        if w_up is not None:
            t_up, *_ = scene_bvh.ray_cast(p.location + margin, up)
            z = min(w_up.z, t_up.z) if t_up is not None else w_up.z
            z = max(
                p.location.z, z - 0.7
            )  # the origin will be the creature's foot, allow some space for the rest of it
            p.location.z = np.random.uniform(p.location.z, z)
