# ----------------------------------------------------------------------------
# The print pipeline against a real WMV export, inside a real Blender.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Everything here needs geometry a fixture cannot fake: empty objects left by
# separate-by-material, seams that are open by construction, a voxel remesh with
# a real cost, and a written STL whose size is the only proof the unit did not
# slip. All three bugs this file has caught so far were of that kind.
#
#   blender -b --factory-startup --python tests/test_print_pipeline.py \
#           -- <character.fbx> [output-dir]
#
# Exits non-zero on failure, so it can be wired into a build.
# ----------------------------------------------------------------------------

import importlib.util
import os
import sys
import tempfile

import bpy

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     os.pardir, "io_import_wmv_fbx", "__init__.py")


def load_addon():
    spec = importlib.util.spec_from_file_location("io_import_wmv_fbx", ADDON)
    module = importlib.util.module_from_spec(spec)
    sys.modules["io_import_wmv_fbx"] = module
    spec.loader.exec_module(module)
    module.register()
    return module


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: ... --python test_print_pipeline.py -- <character.fbx> "
              "[output-dir]")
        sys.exit(2)
    out = argv[1] if len(argv) > 1 else tempfile.mkdtemp(prefix="wmv_print_")
    if not os.path.isdir(out):
        os.makedirs(out)
    return argv[0], out


wmv = load_addon()
FBX, OUTDIR = parse_args()
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-54s %s" % (label, "ok" if ok else "FAILED: %r != %r" % (got, want)))
    if not ok:
        failures.append(label)


def call(operator, **kwargs):
    """Run an operator and normalise its two ways of refusing.

    An operator that reports ERROR makes bpy.ops raise RuntimeError instead of
    returning CANCELLED. Both are refusals; only the caller that knows this
    survives them, which is why the chain operator catches them too."""
    try:
        return "/".join(sorted(operator(**kwargs)))
    except RuntimeError:
        return "CANCELLED"


def meshes():
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def triangles(obj):
    return sum(len(polygon.vertices) - 2 for polygon in obj.data.polygons)


def fresh(**properties):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    wmv.import_wmv_fbx(FBX, effect_planes="hide_static")
    for key, value in properties.items():
        setattr(bpy.context.scene, key, value)


def _print_parts():
    """The cut pieces, in the order they were made."""
    return sorted((o for o in meshes() if o.name.startswith("WMV_Druck_Teil")),
                  key=lambda o: o.name)


def _figure_mm():
    """Height of the visible geometry in scene millimetres."""
    zs = [v.co.z for o in meshes() for v in o.data.vertices]
    return (max(zs) - min(zs)) * 1000.0 if zs else 0.0


def strip_stamps():
    for material in bpy.data.materials:
        for key in [k for k in material.keys() if k.startswith("wmv_")]:
            del material[key]


print("\n=== An empty scene is refused, not waved through")
bpy.ops.wm.read_factory_settings(use_empty=True)
check("print_check", call(bpy.ops.wmv.print_check), "CANCELLED")
check("print_strip", call(bpy.ops.wmv.print_strip), "CANCELLED")
check("print_unify", call(bpy.ops.wmv.print_unify), "CANCELLED")

print("\n=== Without material stamps step 1 stops instead of doing nothing quietly")
fresh()
strip_stamps()
check("print_strip without sidecar", call(bpy.ops.wmv.print_strip), "CANCELLED")

print("\n=== The full chain on a real character")
fresh(wmv_print_height_mm=200.0, wmv_print_feature_mm=0.4,
      wmv_print_remesh=True, wmv_print_max_tris=1000000)
before = len(meshes())
check("step 1 strip", call(bpy.ops.wmv.print_strip), "FINISHED")
check("effect planes gone", len(meshes()) < before, True)
check("step 2 freeze", call(bpy.ops.wmv.print_freeze), "FINISHED")
# The pose has to be baked into the vertices and the skeleton gone, or the STL
# exports the bind pose with a 90-degree overhang down each arm.
check("no armatures left",
      [o for o in bpy.context.scene.objects if o.type == "ARMATURE"], [])
check("no modifiers left", sum(len(o.modifiers) for o in meshes()), 0)
check("scale applied everywhere",
      [o.name for o in meshes()
       if any(abs(c - 1.0) > 1e-4 for c in o.matrix_world.to_scale())], [])
check("step 3 thicken", call(bpy.ops.wmv.print_thicken), "FINISHED")
check("step 4 unify", call(bpy.ops.wmv.print_unify), "FINISHED")
check("one body left", len(meshes()), 1)
check("step 5 decimate", call(bpy.ops.wmv.print_decimate), "FINISHED")
check("under the triangle limit", triangles(meshes()[0]) <= 1000000, True)

stl = os.path.join(OUTDIR, "pipeline_200mm.stl")
check("step 6 export", call(bpy.ops.wmv.print_export, filepath=stl), "FINISHED")

# The acceptance test is the file. STL carries no unit, so a factor mistake
# writes a perfectly valid solid of the wrong size and nothing complains.
measured = wmv._stl_bounds(stl)
check("STL readable", measured is not None, True)
if measured:
    count, (dx, dy, dz) = measured["triangles"], measured["size"]
    contact_x, contact_y, _ = measured["contact"]
    print("     %d triangles, %.2f x %.2f x %.2f mm, %.1f MB, contact %.1f x %.1f mm"
          % (count, dx, dy, dz, os.path.getsize(stl) / 1048576.0,
             contact_x, contact_y))
    # The figure has to stand on the plate, not straddle the origin:
    # a slicer clips or silently drops whatever is below z=0.
    check("sits on the plate", round(measured["floor"], 3), 0.0)
    # A standing pose can touch the plate on a single toe; PrusaSlicer
    # answers that with no file at all, so the patch is measured here.
    check("contact patch reported", contact_x > 0.0 and contact_y > 0.0, True)
    check("height hit exactly", round(dz, 1), 200.0)

# Watertight is what a slicer needs; the seams at wrist and ankle are open in
# the source and only the remesh closes them.
import bmesh  # noqa: E402  -- only needed once the pipeline has run
bm = bmesh.new()
bm.from_mesh(meshes()[0].data)
check("no boundary edges", sum(1 for e in bm.edges if len(e.link_faces) == 1), 0)
check("no non-manifold edges", sum(1 for e in bm.edges if len(e.link_faces) > 2), 0)
bm.free()

print("\n=== Remesh and decimation can be switched off")
fresh(wmv_print_height_mm=200.0, wmv_print_feature_mm=0.4,
      wmv_print_remesh=False, wmv_print_max_tris=0)
bpy.ops.wmv.print_strip()
bpy.ops.wmv.print_freeze()
bpy.ops.wmv.print_thicken()
check("unify without remesh", call(bpy.ops.wmv.print_unify), "FINISHED")
raw = triangles(meshes()[0])
check("decimate with limit 0", call(bpy.ops.wmv.print_decimate), "FINISHED")
check("left untouched", triangles(meshes()[0]), raw)

print("\n=== A voxel far too fine is refused, not run for minutes")
# The feature size clamps at 0.01 -- it has to describe a resin printer's
# pixel pitch as well as a nozzle, so it reaches far below any nozzle.
# The guard therefore exists for the reachable extreme: a large figure at
# the finest feature size.
fresh(wmv_print_height_mm=200.0, wmv_print_feature_mm=0.4)
bpy.ops.wmv.print_strip()
bpy.ops.wmv.print_freeze()
bpy.context.scene.wmv_print_feature_mm = 0.001
check("feature size clamps to its minimum",
      round(bpy.context.scene.wmv_print_feature_mm, 3), 0.01)
# The property's own maximum, so the assertion holds for any character rather
# than for one whose proportions happen to cross the threshold -- at 400 mm the
# orc lands on 3999.6 against a limit of 4000 and legitimately passes.
bpy.context.scene.wmv_print_height_mm = 2000.0
check("unify refuses 2000 mm at the finest feature size",
      call(bpy.ops.wmv.print_unify), "CANCELLED")
bpy.context.scene.wmv_print_height_mm = 200.0
bpy.context.scene.wmv_print_feature_mm = 0.4
check("same scene runs at 200 mm / 0.4 mm", call(bpy.ops.wmv.print_unify), "FINISHED")

print("\n=== The chain button, and what it says when a step refuses")
fresh(wmv_print_height_mm=150.0, wmv_print_feature_mm=0.4,
      wmv_print_remesh=True, wmv_print_max_tris=1000000)
check("run_all", call(bpy.ops.wmv.print_run_all), "FINISHED")
small = os.path.join(OUTDIR, "pipeline_150mm.stl")
check("export after run_all", call(bpy.ops.wmv.print_export, filepath=small), "FINISHED")
measured = wmv._stl_bounds(small)
check("height hit exactly",
      round(measured["size"][2], 1) if measured else None, 150.0)
# Running it again must not blow up -- the operators have to tolerate a scene
# that is already finished.
check("run_all a second time", call(bpy.ops.wmv.print_run_all), "FINISHED")

fresh()
strip_stamps()
check("run_all refuses and names the step", call(bpy.ops.wmv.print_run_all), "CANCELLED")

print("\n=== Colour: the chart and the full-colour archive, before unifying")
fresh(wmv_print_height_mm=200.0, wmv_print_feature_mm=0.4)
check("paint chart", call(bpy.ops.wmv.print_paint_chart), "FINISHED")
chart = bpy.data.texts.get("WMV-Bemalvorlage")
check("chart written", chart is not None, True)
if chart:
    body = chart.as_string()
    check("chart has hex codes", body.count("#") > 5, True)
    # No manufacturer names: the Vallejo and Citadel tables are not freely
    # licensed, so hex and LAB go out and the matching stays the user's.
    check("no manufacturer colours", "Vallejo" in body and "Citadel" in body, True)
    check("mentioned only as a refusal", "nicht frei lizenziert" in body, True)

archive = os.path.join(OUTDIR, "vollfarbe.zip")
check("colour export", call(bpy.ops.wmv.print_color_export, filepath=archive), "FINISHED")
import zipfile  # noqa: E402
if os.path.exists(archive):
    with zipfile.ZipFile(archive) as handle:
        names = handle.namelist()
    # The textures are packed inside the FBX and have no file on disk. An
    # archive of just OBJ+MTL looks valid and carries no colour at all -- which
    # is what path_mode="COPY" quietly produced before this was fixed.
    check("archive carries textures",
          sum(1 for n in names if n.lower().endswith(".png")) > 0, True)
    check("archive carries the material library",
          any(n.lower().endswith(".mtl") for n in names), True)
    check("archive carries the mesh",
          any(n.lower().endswith(".obj") for n in names), True)

print("\n=== Cutting for the build volume")
fresh(wmv_print_height_mm=400.0, wmv_print_feature_mm=0.6,
      wmv_print_build_z_mm=150.0, wmv_print_max_tris=300000)
# Before unifying there is no single body to cut, and cutting 23 shells would
# produce nonsense -- so it refuses instead.
check("segment refuses before unify", call(bpy.ops.wmv.print_segment), "CANCELLED")
check("run_all", call(bpy.ops.wmv.print_run_all), "FINISHED")
check("segment", call(bpy.ops.wmv.print_segment), "FINISHED")
parts = _print_parts()
# ceil(400/150) = 3. Sorting by connectivity instead of by slab produced 107
# parts on this same character, so the count is asserted, not eyeballed.
check("three parts", len(parts), 3)
tallest = 0.0
for part_obj in parts:
    zs = [v.co.z for v in part_obj.data.vertices]
    tallest = max(tallest, (max(zs) - min(zs)) * 1000.0 * (400.0 / _figure_mm()))
    bm = bmesh.new()
    bm.from_mesh(part_obj.data)
    boundary = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    nonmanifold = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    bm.free()
    check("%s watertight" % part_obj.name, (boundary, nonmanifold), (0, 0))
check("every part fits the build height", tallest <= 150.0 * 1.02, True)

print("\n=== A figure that fits is not cut")
fresh(wmv_print_height_mm=120.0, wmv_print_feature_mm=0.6,
      wmv_print_build_z_mm=250.0, wmv_print_max_tris=300000)
bpy.ops.wmv.print_run_all()
check("segment leaves it alone", call(bpy.ops.wmv.print_segment), "FINISHED")
check("still one body", len(meshes()), 1)

print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("test_print_pipeline: all checks passed")
