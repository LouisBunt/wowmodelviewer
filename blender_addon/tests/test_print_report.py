# ----------------------------------------------------------------------------
# Arithmetic of the 3D-print report, checked without Blender.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# _print_report() and _print_role() touch no scene data, so they run against a
# hand-computed fixture under plain CPython -- no Blender, no FBX, no seconds.
# That split is the point: when the end-to-end run broke on a real character,
# this file staying green located the fault in the measuring half immediately.
#
#   python tests/test_print_report.py
# ----------------------------------------------------------------------------

import io
import os
import sys
import types

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     os.pardir, "io_import_wmv_fbx", "__init__.py")


def load_addon():
    """Execute the add-on's module body with Blender stubbed out.

    The stub classes are distinct types on purpose: bpy.types.Operator and
    ImportHelper both being `object` makes the operator declarations fail with
    a duplicate-base or MRO error rather than anything informative."""
    for name in ("bpy", "bmesh", "bpy.props", "bpy_extras", "bpy_extras.io_utils",
                 "mathutils"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["mathutils"].Matrix = type("Matrix", (), {"Identity": staticmethod(lambda n: None)})
    bpy = sys.modules["bpy"]
    bpy.types = types.SimpleNamespace(Operator=type("Operator", (), {}),
                                      Panel=type("Panel", (), {}))
    bpy.ops = types.SimpleNamespace()
    bpy.props = sys.modules["bpy.props"]
    for prop in ("StringProperty", "BoolProperty", "EnumProperty",
                 "FloatProperty", "IntProperty"):
        setattr(bpy.props, prop, lambda **kwargs: None)
    for helper in ("ImportHelper", "ExportHelper"):
        setattr(sys.modules["bpy_extras.io_utils"], helper, type(helper, (), {}))

    module = types.ModuleType("wmv_addon")
    source = io.open(ADDON, encoding="utf-8").read()
    exec(compile(source, ADDON, "exec"), module.__dict__)
    return module


wmv = load_addon()
failures = []


def check(label, got, want):
    if got != want:
        failures.append("%s: expected %r, got %r" % (label, want, got))


def part(name, bulk_mm, open_edges=0, tris=100, effect=False, two_sided=False,
         hidden=False, z=(0.0, 1830.0), non_manifold=0, edges=300,
         has_stamp=True):
    """One row as _print_measure() would produce it."""
    return {
        "name": name, "tris": tris, "area_mm2": 1000.0, "volume_mm3": 1.0,
        "bulk_mm": bulk_mm, "open_edges": open_edges, "edges": edges,
        "non_manifold": non_manifold, "size_mm": (600.0, 300.0, z[1] - z[0]),
        "x_mm": (-300.0, 300.0), "y_mm": (-150.0, 150.0), "z_mm": z,
        "hidden": hidden, "effect_plane": effect, "two_sided": two_sided,
        "has_stamp": has_stamp,
    }


# A human-sized figure: 1830 mm in the scene, printed at 200 mm, 0.4 nozzle.
SCENE = [
    part("Body", bulk_mm=8.0),
    part("Cloak", bulk_mm=0.0, open_edges=420, z=(400.0, 1500.0)),
    part("Hand", bulk_mm=5.0, open_edges=60, z=(700.0, 1000.0)),
    part("Glow", bulk_mm=0.0, effect=True, two_sided=True),
    part("HiddenPlane", bulk_mm=0.0, effect=True, hidden=True),
]
PROFILE = dict(wmv.PROFILE_DEFAULTS, height_mm=200.0)
lines, roles = wmv._print_report(SCENE, PROFILE)
report = "\n".join(lines)

# --- sorting -----------------------------------------------------------------
check("sheet detected", [m["name"] for m in roles.get("nullflaeche", [])], ["Cloak"])
check("effect plane detected", [m["name"] for m in roles.get("effekt", [])], ["Glow"])
# "Hand" is the wrist seam: open boundary edges but real volume. With
# AMBIGUOUS_IS_SOLID it counts as solid rather than being thickened into a
# mitten -- the mistake that would still slice cleanly and print wrong.
check("ambiguous counts as solid",
      sorted(m["name"] for m in roles.get("geschlossen", [])), ["Body", "Hand"])
check("nothing left undecided", roles.get("unklar"), None)
check("hidden objects excluded", "HiddenPlane" in report, False)

# The shoulder lesson: a part whose boundary edges exceed OPEN_SHEET_RATIO is
# an open shell and needs a wall, however large its (meaningless) bulk reads.
# Measured: the orc's shoulder skulls (52% open, bulk "30 mm") were eaten by
# the remesh until this rule thickened them.
shell = part("SkullPlate", 30.0, open_edges=160, edges=300)
check("open shell is a sheet",
      [m["name"] for m in wmv._print_report([shell], PROFILE)[1].get("nullflaeche", [])],
      ["SkullPlate"])
# ...while the 20%-open wrist seam stays solid (asserted above via "Hand").

wmv.AMBIGUOUS_IS_SOLID = False
check("switch flips ambiguous to undecided",
      [m["name"] for m in wmv._print_report(SCENE, PROFILE)[1].get("unklar", [])],
      ["Hand"])
wmv.AMBIGUOUS_IS_SOLID = True

# --- arithmetic --------------------------------------------------------------
# 200 / 1830 = 0.10929. A 1.2 mm printed wall is 1.2/0.10929 = 10.98 mm of scene
# geometry, i.e. 0.010980 BU at 1 BU = 1 m. The voxel is HALF the feature
# (geometry may be finer than the nozzle): 0.2 mm -> 0.001830 BU.
check("scale", "Massstab 0.1093" in report, True)
check("measured height", "1830.0 mm hoch" in report, True)
check("wall derived from feature", "Wandstaerke 1.20 mm" in report, True)
check("solidify in BU", "0.010980 BU" in report, True)
check("voxel in BU", "0.001830 BU" in report, True)
check("triangles exclude hidden", "Dreiecke gesamt:  400" in report, True)

# A 0.2 nozzle halves both -- the cheapest lever there is.
check("finer feature halves the wall",
      "Wandstaerke 0.60 mm" in "\n".join(wmv._print_report(SCENE, dict(PROFILE, feature_mm=0.2))[0]),
      True)

# --- what the report must say ------------------------------------------------
check("2V/A is qualified, not sold as thickness", "Messartefakte" in report, True)
check("seams explained", "offene Naehte" in report, True)
check("build volume fits at 200 mm", "Passt in den Bauraum" in report, True)
check("build volume refused at 300 mm",
      "Passt NICHT" in "\n".join(
          wmv._print_report(SCENE, dict(PROFILE, height_mm=300.0))[0]), True)
check("no-solid note quiet when something is solid", "taugt damit nicht" in report, False)
check("no-solid note fires when nothing is",
      "taugt damit nicht" in "\n".join(
          wmv._print_report([part("Sheet", 0.0, open_edges=40)], PROFILE)[0]),
      True)
check("missing sidecar called out",
      "wmvmat.json" in "\n".join(
          wmv._print_report([part("Bare", 8.0, has_stamp=False)], PROFILE)[0]),
      True)

# --- the profile -------------------------------------------------------------
# The two-part test rule from DRUCK-IDEEN.md, turned into assertions.

# Part one: throw the profile away. Does the tool still do something sensible?
bare_lines, bare_roles = wmv._print_report(SCENE, {"height_mm": 200.0})
check("survives a profile carrying only the height", bool(bare_roles), True)
check("falls back to the default wall",
      "Wandstaerke 1.20 mm" in "\n".join(bare_lines), True)

# Part two: set the values absurd. Do sensible warnings come out, or does some
# hardcoded number carry on regardless?
absurd = "\n".join(wmv._print_report(
    SCENE, dict(PROFILE, build_x_mm=10.0, build_y_mm=10.0, build_z_mm=10.0,
                feature_mm=5.0))[0])
check("absurd build volume refused", "Passt NICHT" in absurd, True)
check("absurd build volume counts the parts", "Mindestens 20 Teile" in absurd, True)
check("absurd feature drives the wall", "Wandstaerke 15.00 mm" in absurd, True)

# An explicit wall beats the derived one, and the report says which it used.
explicit = "\n".join(wmv._print_report(SCENE, dict(PROFILE, wall_mm=0.8))[0])
check("explicit wall used", "Wandstaerke 0.80 mm" in explicit, True)
check("explicit wall labelled", "aus dem Profil" in explicit, True)

# The three process-shaped numbers, each speaking for itself. There is no branch
# on a process name anywhere in the code they drive, which is the whole point:
# a fourth process nobody has built yet is describable with the same six fields.
powder = "\n".join(wmv._print_report(SCENE, dict(PROFILE, overhang_deg=90.0))[0])
check("overhang 90 means overhangs are irrelevant", "gleichgueltig" in powder, True)
check("overhang below 90 asks for supports", "brauchen Stuetzen" in report, True)
check("tilt mentioned when set",
      "Kippung" in "\n".join(
          wmv._print_report(SCENE, dict(PROFILE, tilt_deg=30.0))[0]), True)
check("tilt silent at zero", "Kippung" in report, False)
check("escape hole mentioned when set",
      "Austrittsloecher" in "\n".join(
          wmv._print_report(SCENE, dict(PROFILE, escape_hole_mm=3.0))[0]), True)
check("escape hole silent at zero", "Austrittsloecher" in report, False)

# The plate is matched largest-to-largest, because the figure may be turned on
# it. Anything else reports a false failure for a figure that only needs
# rotating by ninety degrees.
wide = [part("Wide", 8.0, z=(0.0, 1000.0))]
wide[0]["x_mm"] = (-1000.0, 1000.0)
wide[0]["y_mm"] = (-500.0, 500.0)
turned = "\n".join(wmv._print_report(
    wide, dict(PROFILE, height_mm=100.0, build_x_mm=120.0, build_y_mm=220.0))[0])
check("plate matched largest-to-largest", "Passt in den Bauraum" in turned, True)


# --- colour arithmetic -------------------------------------------------------
# CIELAB against known values. The whole reason the chart is in LAB rather than
# RGB is that the numbers mean something perceptual; numbers that mean something
# can be checked, which RGB-distance guesswork could not be.


def close(got, want, tol=0.05):
    return abs(got - want) <= tol


white = wmv._linear_to_lab((1.0, 1.0, 1.0))
check("white is L*100", close(white[0], 100.0), True)
check("white is neutral", close(white[1], 0.0, 0.01) and close(white[2], 0.0, 0.01), True)

black = wmv._linear_to_lab((0.0, 0.0, 0.0))
check("black is L*0", close(black[0], 0.0), True)

# Mid grey in sRGB (#808080) is linear 0.2158; its L* is the classic 53.6.
grey = wmv._linear_to_lab((0.2158605, 0.2158605, 0.2158605))
check("sRGB mid grey is L*53.6", close(grey[0], 53.6, 0.15), True)
check("mid grey is neutral", close(grey[1], 0.0, 0.01), True)

# Pure red: L*53.24, a*80.09, b*67.20 -- the textbook sRGB primary.
red = wmv._linear_to_lab((1.0, 0.0, 0.0))
check("red L*", close(red[0], 53.24, 0.1), True)
check("red a*", close(red[1], 80.09, 0.2), True)
check("red b*", close(red[2], 67.20, 0.2), True)

# The transfer function, and the two ends of the hex conversion.
check("linear 0 stays 0", close(wmv._linear_to_srgb(0.0), 0.0, 1e-9), True)
check("linear 1 stays 1", close(wmv._linear_to_srgb(1.0), 1.0, 1e-9), True)
check("linear 0.2159 is sRGB 0.5", close(wmv._linear_to_srgb(0.2158605), 0.5, 0.002), True)
check("hex white", wmv._srgb_to_hex((1.0, 1.0, 1.0)), "#FFFFFF")
check("hex black", wmv._srgb_to_hex((0.0, 0.0, 0.0)), "#000000")
# Out-of-range values are clamped rather than producing a broken hex string:
# a linear average can land slightly above 1 on an emissive texture.
check("hex clamps overshoot", wmv._srgb_to_hex((1.4, -0.2, 0.5)), "#FF0080")


# --- where to cut ------------------------------------------------------------
# The cut is geometric, not anatomical, because the FBX names bones by index
# (bone_2, bone_17) with no anatomy attached. What it must do is find the
# narrowest cross-section near each even division.

class _Vert(object):
    """Just enough of a BMVert for _segment_heights."""

    def __init__(self, x, y, z):
        self.co = types.SimpleNamespace(x=x, y=y, z=z)


def _figure(waist_z, waist_half=0.05, body_half=1.0, steps=400):
    """A column 0..10 tall that pinches to a narrow waist at waist_z."""
    verts = []
    for step in range(steps + 1):
        z = 10.0 * step / steps
        half = waist_half if abs(z - waist_z) < 0.25 else body_half
        for x in (-half, half):
            for y in (-half, half):
                verts.append(_Vert(x, y, z))
    return verts


# One cut wanted, ideal position 5.0, actual pinch at 5.4 -- inside the search
# window, so the cut must move to it rather than sitting on the ideal.
found = wmv._segment_heights(_figure(5.4), 0.0, 10.0, 2)
check("one cut for two parts", len(found), 1)
check("cut moves to the narrow spot", abs(found[0] - 5.4) < 0.25, True)

# A pinch outside the window must NOT drag the cut there: parts that drift too
# far from equal height stop fitting the build volume, which is the failure the
# search window exists to prevent.
far = wmv._segment_heights(_figure(9.0), 0.0, 10.0, 2)
check("cut stays near the even division when the pinch is far off",
      abs(far[0] - 5.0) < 0.8, True)

check("no cuts for a single part", wmv._segment_heights(_figure(5.0), 0.0, 10.0, 1), [])
check("three parts need two cuts",
      len(wmv._segment_heights(_figure(5.0), 0.0, 10.0, 3)), 2)


# --- degenerate input --------------------------------------------------------
check("empty scene", wmv._print_report([], PROFILE)[1], {})
check("scene with no height",
      wmv._print_report([part("Flat", 0.0, z=(5.0, 5.0))], PROFILE)[1], {})

if failures:
    print("FAILED:")
    for failure in failures:
        print("  -", failure)
    sys.exit(1)
print("test_print_report: all checks passed")
