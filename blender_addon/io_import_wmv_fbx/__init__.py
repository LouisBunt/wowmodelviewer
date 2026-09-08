# ----------------------------------------------------------------------------
# WoW Model Viewer: Midnight -- Blender FBX importer add-on.
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Part of better Model Viewer, distributed under the GNU General Public License
# version 3 or later -- the same licence Blender add-ons require anyway.
# Source: https://github.com/LouisBunt/wowmodelviewer-qt
#
# Imports an FBX exported by WoW Model Viewer: Midnight and rebuilds every
# material's node graph from the ".wmvmat.json" sidecar the exporter writes,
# so the imported model matches the WMV viewport exactly:
#
#   blend 0 (opaque)          -> opaque Principled BSDF
#   blend 1 (alpha test)      -> alpha clip at 0.5
#   blend 2 (alpha blend)     -> alpha blended from texture alpha
#   blend 3/4 (additive glow) -> emission, transparency driven by the file's
#                                alpha (which the exporter bakes as per-pixel
#                                brightness: black = invisible, like in-game)
#   blend 5/6 (modulate)      -> multiply-style blend approximation
#   unlit                     -> emission instead of diffuse shading
#
# Geometry, armature, skin weights and animation come from Blender's own FBX
# importer; this add-on only replaces the material setup, which is the part a
# generic FBX round-trip cannot preserve. Without a sidecar the import still
# works and simply keeps Blender's default materials.
# ----------------------------------------------------------------------------

bl_info = {
    "name": "WoW Model Viewer FBX (.fbx)",
    "author": "WoW Model Viewer: Midnight",
    "version": (1, 3, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > WoW Model Viewer FBX (.fbx); 3D View > Sidebar > WMV",
    "description": "Import WMV-exported FBX with viewport-identical materials, "
                   "and prepare it for 3D printing",
    "category": "Import-Export",
}

import json
import os
import re
import struct
import tempfile
import zipfile

import bmesh
import bpy
from mathutils import Matrix
from bpy.props import (StringProperty, BoolProperty, EnumProperty,
                       FloatProperty, IntProperty)
from bpy_extras.io_utils import ImportHelper, ExportHelper

# Raw M2 blend modes, mirrored from the exporter.
BM_OPAQUE = 0
BM_ALPHA_TEST = 1
BM_ALPHA_BLEND = 2
BM_ADDITIVE = 3
BM_ADDITIVE_ALPHA = 4
BM_MODULATE = 5
BM_MODULATE2X = 6

# Blender appends ".001"-style suffixes when names collide.
_DEDUP_SUFFIX = re.compile(r"\.\d{3,}$")

# Names over Blender's 63-char limit get truncated with a "_<7 hex>" hash tail.
_TRUNCATION_HASH = re.compile(r"_[0-9a-f]{7}$")


def _base_name(name):
    return _DEDUP_SUFFIX.sub("", name)


def _match_sidecar_entry(sidecar, material_name):
    """Match a Blender material name to a sidecar entry.

    Tries, in order: exact name; name with Blender's ".00N" de-dup suffix
    stripped; and -- for names Blender truncated to its 63-character limit
    (recognizable by the "_<hash>" tail) -- a unique prefix match against the
    sidecar names. Returns None when no unambiguous match exists.
    """
    base = _base_name(material_name)
    entry = sidecar.get(base) or sidecar.get(material_name)
    if entry is not None:
        return entry

    truncated = _TRUNCATION_HASH.sub("", base)
    if truncated != base:
        candidates = [e for name, e in sidecar.items() if name.startswith(truncated)]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _load_sidecar(fbx_path):
    """Load '<fbx>.wmvmat.json'. Returns the raw sidecar dict (version 1 = bake mode, version 2 =
    component / raw node-based mode with per-unit data) or None."""
    sidecar_path = fbx_path + ".wmvmat.json"
    if not os.path.isfile(sidecar_path):
        return None
    try:
        with open(sidecar_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        print("WMV import: unreadable sidecar %s (%s)" % (sidecar_path, exc))
        return None
    if data.get("version") not in (1, 2):
        print("WMV import: unsupported sidecar version %r" % data.get("version"))
        return None
    return data


def _stamp_wmv_properties(material, entry):
    """Record the WMV pass data as material custom properties, whatever the
    glow mode -- artists building their own shaders can read blend mode /
    unlit / two-sided straight from the material's Custom Properties panel."""
    material["wmv_blend_mode"] = int(entry.get("blendMode", BM_OPAQUE))
    material["wmv_unlit"] = bool(entry.get("unlit", False))
    material["wmv_two_sided"] = bool(entry.get("twoSided", False))
    material["wmv_additive"] = int(entry.get("blendMode", BM_OPAQUE)) in (
        BM_ADDITIVE, BM_ADDITIVE_ALPHA)
    material["wmv_texture"] = str(entry.get("texture", ""))


def _get_scroll(entry):
    """The material's UV-scroll track: v1 sidecars carry it per material, v2 per
    texture unit -- prefer the unit data where present."""
    for unit in entry.get("units", []):
        if unit.get("texScroll"):
            return unit["texScroll"]
    return entry.get("texScroll")


def _add_uv_scroll(material, image_node, scroll):
    """Drive the image's UVs with a constant scroll so frozen effect sheets animate.

    A Mapping node ahead of the image gets drivers on Location X/Y with the
    expression '<rate> * frame'. A constant times the builtin `frame` counts as a
    "simple expression", which Blender evaluates WITHOUT the auto-run-Python
    permission -- the same trick wow.export's TexturePanner uses. The frame rate is
    baked into the constant at import time; the sidecar's period is in milliseconds.
    """
    period = float(scroll.get("period_ms", 0))
    if period <= 0.0:
        return False
    fps = bpy.context.scene.render.fps or 30
    rate_u = float(scroll.get("dx", 0.0)) / period * (1000.0 / fps)
    rate_v = float(scroll.get("dy", 0.0)) / period * (1000.0 / fps)
    if rate_u == 0.0 and rate_v == 0.0:
        return False

    tree = material.node_tree
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.location = (image_node.location.x - 400, image_node.location.y)
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.vector_type = "POINT"
    mapping.location = (image_node.location.x - 220, image_node.location.y)
    tree.links.new(uv.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], image_node.inputs["Vector"])

    for axis, rate in ((0, rate_u), (1, rate_v)):
        if rate == 0.0:
            continue
        fcurve = mapping.inputs["Location"].driver_add("default_value", axis)
        fcurve.driver.type = "SCRIPTED"
        fcurve.driver.expression = "%.10f * frame" % rate
    return True


def _find_image_node(material):
    """The Image Texture node Blender's stock FBX importer wired up. Reused rather than
    re-loaded: for an embedded-texture FBX the image datablock lives inside the .blend,
    and loading the loose PNG again would silently pick the wrong (or no) file."""
    if not material.use_nodes:
        return None
    for node in material.node_tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node
    return None


def _set_blend_method(material, method, clip_threshold=0.5):
    """Viewport alpha mode across the 4.2 API break.

    Up to 4.1 this is material.blend_method ('OPAQUE'/'CLIP'/'BLEND'). EEVEE Next
    (4.2+) replaced it with surface_render_method ('DITHERED'/'BLENDED') and dropped
    the explicit clip mode -- alpha-clip materials render via DITHERED there, which
    is visually equivalent for hard-edged WoW alpha tests."""
    if bpy.app.version >= (4, 2, 0):
        material.surface_render_method = "BLENDED" if method == "BLEND" else "DITHERED"
    else:
        material.blend_method = method
        if method == "CLIP":
            material.alpha_threshold = clip_threshold


def _build_material_nodes(material, entry):
    """Rebuild the node graph so the import matches the WMV viewport.

    blend 0 -> opaque Principled; 1 -> alpha clip 0.5; 2 -> alpha blend;
    3/4 -> emission over transparency, mixed by the texture's alpha (the exporter
    bakes brightness-as-alpha for those files); 5/6 -> multiply approximation
    (transparency weighted by darkness); unlit -> emission instead of diffuse.
    twoSided drives backface culling. Returns False when the material has no image
    node to build from (the graph is then left untouched)."""
    image_node = _find_image_node(material)
    if image_node is None:
        return False

    blend = int(entry.get("blendMode", BM_OPAQUE))
    unlit = bool(entry.get("unlit", False))
    emissive = entry.get("emissive", [1.0, 1.0, 1.0])

    tree = material.node_tree
    nodes = tree.nodes
    links = tree.links

    # Keep only the image and the output; everything else (the stock Principled and
    # whatever it dragged in) is rebuilt from the sidecar's truth.
    #
    # Compared BY NAME, not identity: bpy hands out a fresh Python wrapper per access,
    # so `node is not image_node` can be True for the image node itself -- which then
    # got removed here and left image_node dangling (KeyError on its sockets at best,
    # a hard access violation at worst).
    output = None
    for node in list(nodes):
        if node.type == "OUTPUT_MATERIAL":
            output = node
        elif node.name != image_node.name:
            nodes.remove(node)
    if output is None:
        output = nodes.new("ShaderNodeOutputMaterial")
    image_node.location = (-600, 0)
    output.location = (400, 0)

    def new(node_type, x, y):
        node = nodes.new(node_type)
        node.location = (x, y)
        return node

    additive = blend in (BM_ADDITIVE, BM_ADDITIVE_ALPHA)
    modulate = blend in (BM_MODULATE, BM_MODULATE2X)

    if additive or (unlit and blend in (BM_ALPHA_BLEND,)):
        # Emission over transparency: the exporter baked this pass's framebuffer
        # contribution into RGB and its brightness into alpha, so the mix over a
        # Transparent BSDF reproduces the game's additive stacking.
        emission = new("ShaderNodeEmission", -200, 100)
        emission.inputs["Color"].default_value = (*emissive[:3], 1.0) if unlit else (1, 1, 1, 1)
        links.new(image_node.outputs["Color"], emission.inputs["Color"])
        transparent = new("ShaderNodeBsdfTransparent", -200, -150)
        mix = new("ShaderNodeMixShader", 100, 0)
        links.new(image_node.outputs["Alpha"], mix.inputs[0])
        links.new(transparent.outputs["BSDF"], mix.inputs[1])
        links.new(emission.outputs["Emission"], mix.inputs[2])
        links.new(mix.outputs["Shader"], output.inputs["Surface"])
        _set_blend_method(material, "BLEND")
    elif modulate:
        # Multiply blending has no exact Eevee/Cycles node: approximate by making
        # dark texel areas transparent -- visually close for the shadow/stain decals
        # this mode is used for.
        transparent = new("ShaderNodeBsdfTransparent", -200, -150)
        links.new(image_node.outputs["Color"], transparent.inputs["Color"])
        links.new(transparent.outputs["BSDF"], output.inputs["Surface"])
        _set_blend_method(material, "BLEND")
    elif unlit:
        emission = new("ShaderNodeEmission", -200, 0)
        links.new(image_node.outputs["Color"], emission.inputs["Color"])
        if blend in (BM_ALPHA_TEST, BM_ALPHA_BLEND):
            transparent = new("ShaderNodeBsdfTransparent", -200, -200)
            mix = new("ShaderNodeMixShader", 100, 0)
            links.new(image_node.outputs["Alpha"], mix.inputs[0])
            links.new(transparent.outputs["BSDF"], mix.inputs[1])
            links.new(emission.outputs["Emission"], mix.inputs[2])
            links.new(mix.outputs["Shader"], output.inputs["Surface"])
            _set_blend_method(material, "CLIP" if blend == BM_ALPHA_TEST else "BLEND")
        else:
            links.new(emission.outputs["Emission"], output.inputs["Surface"])
    else:
        principled = new("ShaderNodeBsdfPrincipled", -200, 0)
        # WoW materials are not glossy plastic; kill the default specular highlight.
        # 4.0 renamed the input, so address it defensively.
        for spec_name in ("Specular IOR Level", "Specular"):
            if spec_name in principled.inputs:
                principled.inputs[spec_name].default_value = 0.0
                break
        links.new(image_node.outputs["Color"], principled.inputs["Base Color"])
        if blend in (BM_ALPHA_TEST, BM_ALPHA_BLEND):
            links.new(image_node.outputs["Alpha"], principled.inputs["Alpha"])
            _set_blend_method(material, "CLIP" if blend == BM_ALPHA_TEST else "BLEND")
        links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    material.use_backface_culling = not bool(entry.get("twoSided", False))

    # Last, ON PURPOSE: the rebuild above removes every node except image and output,
    # so the scroll chain has to be added after it.
    scroll = _get_scroll(entry)
    if scroll:
        material["wmv_has_scroll"] = _add_uv_scroll(material, image_node, scroll)
    return True


def _is_effect_plane(entry):
    """A translucent, unlit glow pass (alpha-blend or additive) is a WoW particle/effect billboard --
    e.g. an artifact weapon's frost/energy sheets. In-game these are animated and scroll; frozen into
    static geometry they become big flat quads with hard rectangular edges. Detected so the importer
    can optionally hide them (they're rarely wanted in a static export). A SOLID glow (blend 0, opaque
    -- a rune/emissive detail baked onto the weapon) is NOT an effect plane and stays visible.

    Sidecars written before the exporter carried the classification in bake mode lack
    isGlow/alphaUsage entirely -- 'Hide effect planes' was silently a no-op for them.
    Both values are derivable from fields every sidecar version has, so derive them."""
    blend = int(entry.get("blendMode", BM_OPAQUE))
    unlit = bool(entry.get("unlit", False))

    is_glow = entry.get("isGlow")
    if is_glow is None:
        is_glow = unlit or blend in (BM_ADDITIVE, BM_ADDITIVE_ALPHA)

    alpha = entry.get("alphaUsage")
    if alpha is None:
        alpha = {BM_ALPHA_TEST: "alpha_clip", BM_ALPHA_BLEND: "alpha_blend",
                 BM_MODULATE: "alpha_blend", BM_MODULATE2X: "alpha_blend",
                 BM_ADDITIVE: "additive", BM_ADDITIVE_ALPHA: "additive"}.get(blend, "opaque")

    return bool(is_glow) and unlit and alpha in ("alpha_blend", "additive")


def _collect_imported_materials(objects):
    materials = {}
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material is not None:
                materials[slot.material.name] = slot.material
    return materials


def _separate_by_material(objects):
    """Split each imported multi-material mesh into one object per material, named after the
    material. A WoW model comes through as ONE mesh with many material slots, so isolating a part
    (e.g. hiding an effect plane) otherwise means a manual Edit-Mode 'Separate by Material'. Blender's
    separate preserves the armature modifier, parenting and vertex groups on every piece, so skinning
    still works. Returns the resulting mesh objects."""
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    result = []
    for obj in list(objects):
        if obj.type != "MESH":
            continue
        if len(obj.data.materials) <= 1:
            result.append(obj)
            continue
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        before = set(bpy.data.objects)
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.separate(type="MATERIAL")
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception as exc:
            print("WMV import: separate-by-material failed for %r (%s)" % (obj.name, exc))
            result.append(obj)
            continue
        pieces = [o for o in bpy.data.objects if o not in before]
        pieces.append(obj)
        for piece in pieces:
            bpy.ops.object.select_all(action="DESELECT")
            piece.select_set(True)
            bpy.context.view_layer.objects.active = piece
            try:
                bpy.ops.object.material_slot_remove_unused()
            except Exception:
                pass
            mat = piece.data.materials[0] if piece.data.materials else None
            if mat is not None:
                piece.name = mat.name
                piece.data.name = mat.name
            result.append(piece)
    return result


def import_wmv_fbx(filepath, separate_materials=True, hide_effect_planes=False,
                  build_materials=True, effect_planes=None):
    """Import a WMV FBX. Returns (imported object count, rebuilt material count,
    sidecar found).

    With build_materials (the default) every material whose sidecar entry is found gets
    its node graph rebuilt to match the WMV viewport -- glow as emission, alpha modes,
    backface culling. Without it, materials stay exactly as Blender's stock FBX importer
    creates them and only the render-state metadata is stamped as custom properties, for
    artists who build their own shaders.

    effect_planes: "show" | "hide_static" | "hide_all". hide_static hides only frozen
    sheets WITHOUT a scroll track -- animated ones now live and stay visible. The old
    hide_effect_planes bool maps to hide_all for script compatibility."""
    if effect_planes is None:
        effect_planes = "hide_all" if hide_effect_planes else "show"
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=filepath)
    imported = [obj for obj in bpy.data.objects if obj not in before]

    rebuilt = 0
    data = _load_sidecar(filepath)
    if data:
        sidecar = {entry["name"]: entry for entry in data.get("materials", [])}
        for mat_name, material in _collect_imported_materials(imported).items():
            entry = _match_sidecar_entry(sidecar, mat_name)
            if entry is not None:
                # Stamped regardless of the node work: 'Hide effect planes' and custom
                # tooling read these properties, not the graph.
                _stamp_wmv_properties(material, entry)
                material["wmv_effect_plane"] = _is_effect_plane(entry)
                if build_materials and _build_material_nodes(material, entry):
                    rebuilt += 1
            else:
                print("WMV import: no sidecar entry for material '%s'" % mat_name)

    # Split each model into one object per material so parts (armor, effect planes, glows) can be
    # selected/hidden directly instead of via a manual Edit-Mode separate.
    if separate_materials:
        imported = _separate_by_material(imported)

    # Optionally hide the frozen particle/effect billboards. hide_static keeps the ones
    # that scroll -- those animate now and are usually WANTED. Hidden, not deleted, so
    # un-hiding in the outliner brings them back. Only meaningful when split into
    # per-material objects; otherwise a whole merged mesh would hide.
    if effect_planes != "show" and separate_materials:
        hidden = 0
        for obj in imported:
            if obj.type != "MESH":
                continue
            mats = [m for m in obj.data.materials if m is not None]
            is_plane = any(m.get("wmv_effect_plane") for m in mats)
            animated = any(m.get("wmv_has_scroll") for m in mats)
            if is_plane and (effect_planes == "hide_all" or not animated):
                obj.hide_viewport = True
                obj.hide_render = True
                hidden += 1
        if hidden:
            print("WMV import: hid %d effect-plane object(s)" % hidden)

    return len(imported), rebuilt, data is not None


class IMPORT_SCENE_OT_wmv_fbx(bpy.types.Operator, ImportHelper):
    """Import a WoW Model Viewer FBX with viewport-identical materials"""

    bl_idname = "import_scene.wmv_fbx"
    bl_label = "Import WMV FBX"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})

    effect_planes: EnumProperty(
        name="Effektflächen",
        description="Was mit den Partikel-/Effektflächen passiert",
        items=(
            ("show", "Alle zeigen", "Auch eingefrorene Effektflächen bleiben sichtbar"),
            ("hide_static", "Nur starre verstecken",
             "Flächen mit UV-Animation laufen weiter; nur wirklich eingefrorene werden versteckt"),
            ("hide_all", "Alle verstecken", "Jede Effektfläche wird versteckt"),
        ),
        default="hide_static",
    )

    build_materials: BoolProperty(
        name="Materialien automatisch aufbauen",
        description="Baut die Shader-Nodes so, dass der Import wie im Model Viewer aussieht "
                    "(Glühen als Emission, Alpha-Modi, Backface Culling). Abschalten, um "
                    "Blenders Standard-Materialien zu behalten und eigene Shader zu bauen",
        default=True,
    )

    def execute(self, context):
        object_count, rebuilt, had_sidecar = import_wmv_fbx(
            self.filepath, effect_planes=self.effect_planes,
            build_materials=self.build_materials)
        if not had_sidecar:
            self.report(
                {"WARNING"},
                "No .wmvmat.json sidecar next to the FBX -- imported without "
                "WoW render-state properties. Re-export from WoW Model Viewer to get it.",
            )
        else:
            self.report(
                {"INFO"},
                "Imported %d objects, rebuilt %d materials" % (object_count, rebuilt),
            )
        return {"FINISHED"}


def _menu_entry(self, context):
    self.layout.operator(IMPORT_SCENE_OT_wmv_fbx.bl_idname,
                         text="WoW Model Viewer FBX (.fbx)")


def _last_export_path():
    """The handshake file WoW Model Viewer writes after every FBX export.

    The path is the whole contract -- no IPC; the app hardcodes the same location
    (ExportController.cpp, lastExportFilePath()). One 'FBX:<absolute path>' line."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "WMVMidnight", "last_export.txt")


def _read_last_export():
    """Returns the FBX path from the handshake file, or None."""
    try:
        with open(_last_export_path(), "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("FBX:"):
                    path = line[4:]
                    if os.path.isfile(path):
                        return path
    except OSError:
        pass
    return None


class WMV_OT_import_last_export(bpy.types.Operator):
    """Import the FBX WoW Model Viewer exported last (one click, no file dialog)"""

    bl_idname = "wmv.import_last_export"
    bl_label = "Letzten WMV-Export importieren"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        path = _read_last_export()
        if path is None:
            self.report(
                {"ERROR"},
                "Kein Export gefunden. Erst im Model Viewer \"Als FBX exportieren\" "
                "klicken, dann hier importieren.",
            )
            return {"CANCELLED"}
        object_count, _, had_sidecar = import_wmv_fbx(path, effect_planes="hide_static")
        self.report(
            {"INFO"},
            "%d Objekte aus %s importiert%s" % (
                object_count, os.path.basename(path),
                "" if had_sidecar else " (ohne Sidecar-Metadaten)"),
        )
        return {"FINISHED"}


class WMV_PT_sidebar(bpy.types.Panel):
    """The one-click landing spot: 3D View sidebar (N key), tab 'WMV'."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "WMV"
    bl_label = "WoW Model Viewer"

    def draw(self, context):
        col = self.layout.column()
        col.operator(WMV_OT_import_last_export.bl_idname, icon="IMPORT")
        path = _read_last_export()
        if path is not None:
            col.label(text=os.path.basename(path))
        else:
            col.label(text="Noch kein Export vorhanden.")

        box = self.layout.box()
        box.label(text="3D-Druck", icon="MOD_REMESH")
        box.prop(context.scene, "wmv_print_height_mm")

        profile = box.box()
        profile.label(text="Profil", icon="TOOL_SETTINGS")
        profile.prop(context.scene, "wmv_print_feature_mm")
        profile.prop(context.scene, "wmv_print_wall_mm")
        row = profile.row(align=True)
        row.prop(context.scene, "wmv_print_build_x_mm", text="Bauraum X")
        row.prop(context.scene, "wmv_print_build_y_mm", text="Y")
        row.prop(context.scene, "wmv_print_build_z_mm", text="Z")
        profile.prop(context.scene, "wmv_print_overhang_deg")
        profile.prop(context.scene, "wmv_print_tilt_deg")
        profile.prop(context.scene, "wmv_print_escape_hole_mm")
        row = profile.row(align=True)
        row.operator(WMV_OT_print_profile_load.bl_idname, text="Laden")
        row.operator(WMV_OT_print_profile_save.bl_idname, text="Speichern")
        row.operator(WMV_OT_print_profile_reset.bl_idname, text="Zuruecksetzen")

        box.prop(context.scene, "wmv_print_remesh")
        box.prop(context.scene, "wmv_print_max_tris")
        box.prop(context.scene, "wmv_print_sink_mm")
        box.operator(WMV_OT_print_check.bl_idname, icon="ZOOM_ALL")

        steps = box.column(align=True)
        steps.operator(WMV_OT_print_strip.bl_idname)
        steps.operator(WMV_OT_print_freeze.bl_idname)
        steps.operator(WMV_OT_print_thicken.bl_idname)
        steps.operator(WMV_OT_print_unify.bl_idname)
        steps.operator(WMV_OT_print_decimate.bl_idname)
        steps.operator(WMV_OT_print_export.bl_idname, icon="EXPORT")
        box.operator(WMV_OT_print_run_all.bl_idname, icon="PLAY")
        box.operator(WMV_OT_print_segment.bl_idname, icon="MOD_BEVEL")

        colour = box.box()
        colour.label(text="Farbe", icon="COLOR")
        colour.label(text="Vor dem Vereinigen ausfuehren.")
        colour.operator(WMV_OT_print_paint_chart.bl_idname)
        colour.operator(WMV_OT_print_color_export.bl_idname)


# ---------------------------------------------------------------------------
# 3D print preparation, step zero: measuring.
#
# Nothing in this section changes a single vertex. It measures what a print
# pipeline would have to deal with and reports it in millimetres -- because
# every number that matters downstream (wall thickness, voxel size, whether a
# part is a hollow sheet or a solid) depends on a scale that is only knowable
# once the figure's real height has been measured in the scene.
#
# Two facts here were settled by measuring rather than by assuming, and both
# contradict the original plan in DRUCK-KONZEPT.md:
#
#   * The FBX declares its unit -- SetSystemUnit(FbxSystemUnit(1.0)) in
#     FBXHeaders.cpp:102 -- so Blender imports a WMV character metrically:
#     1 Blender unit = 1 m, a human lands at roughly 1.83 BU. The conversion to
#     millimetres is therefore x1000. NOT the exporter's x91.44 yard-to-
#     centimetre factor, and not the "x10 correction" an earlier draft called
#     for; that correction would have produced an 18 mm figure.
#   * "Solidify where the material is two-sided" cannot work. wmv_two_sided is
#     the raw M2 RENDERFLAGS_TWOSIDED culling flag, not a statement about
#     geometry -- and on a real character every two-sided object is ALSO an
#     effect plane, deleted one step earlier, leaving the rule with nothing to
#     act on. Thickness is therefore measured here (2 x volume / area), never
#     inferred from a render flag.
#
# One thing this step deliberately does not do is guess. A part it cannot place
# with confidence is reported as "unklar" with its raw numbers attached, so the
# decision gets made against measurements instead of ahead of them.
# ---------------------------------------------------------------------------

# 1 Blender unit = 1 metre for a WMV import (see above). Everything user-facing
# is millimetres, so this factor appears exactly once.
BU_TO_MM = 1000.0

# Below this bulk (2 x volume / area) a part encloses no volume at all and is a
# single-layer sheet -- nothing a slicer could turn into a contour. Measured on a
# real character, cloak and tabard come out at exactly 0.000 and everything else
# is above 0.28, so the threshold sits in a gap two orders of magnitude wide.
SHEET_MM = 0.05

# What happens to a part that is neither an unambiguous sheet nor watertight.
# True leaves it alone (see _classify_ambiguous for why that is the safer
# mistake); False reports it as "unklar" and thickens nothing.
AMBIGUOUS_IS_SOLID = True

# A part whose boundary edges are more than this share of all its edges is an
# open shell -- sheet-like armour, not a solid. Set by evidence, not theory:
# the orc's shoulder plates (52% open) went unthickened, and the voxel remesh
# perforated their skull faces into fringe; with a wall they print intact,
# verified in side-by-side renders. The sword falls in the same bucket (35%)
# and simply comes out sturdier.
OPEN_SHEET_RATIO = 0.3

# Voxel size as a fraction of the smallest feature. Half, because geometry may
# be FINER than the nozzle: perimeters follow contours below the nozzle width,
# so a 0.2 mm voxel visibly sharpens a 0.4 mm print. Compared side by side on
# the orc; the cost is file size and remesh time, not print time.
VOXEL_OF_FEATURE = 0.5

# Disconnected fragments smaller than this (printed mm) are swept up after the
# remesh -- solidified scraps of open shells otherwise survive as floating
# grains that print as loose debris hanging in the supports. Anything that is a
# SEPARATE island below this size is either internal (eyes, teeth -- invisible
# and expendable) or debris; a real feature of the figure is connected, or it
# would print loose anyway. 2 mm let 3-4 mm scraps through, seen in renders.
CRUMB_MM = 5.0

# Below this, the figure's footprint on the plate is too small to hold it.
# Not a number picked for looks: PrusaSlicer refused a figure standing on a
# 3.4 x 2.6 mm toe outright, and took one standing on 230 x 116 mm without
# comment. Five sits between the two, low enough that a well-posed figure never
# trips it.
CONTACT_MIN_MM = 5.0


def _print_measure(obj, depsgraph):
    """Measure one mesh object as it would actually be exported -- modifiers
    evaluated (so the armature's pose is included) and in world space (so a
    scale sitting on the object or on its armature parent is accounted for).

    Returns a dict in millimetres, or None if the object carries no geometry."""
    bm = bmesh.new()
    try:
        try:
            bm.from_object(obj, depsgraph)
        except ValueError:
            # "no usable mesh data": _separate_by_material leaves one empty
            # object behind for every material slot that had no faces assigned
            # to it, and from_object raises on those rather than handing back an
            # empty mesh. Seen on a real character, never on a fixture.
            return None
        bm.transform(obj.matrix_world)
        if not bm.faces:
            return None

        area = sum(face.calc_area() for face in bm.faces)
        # Unsigned, because an open sheet has no inside -- for those the number
        # is meaningless anyway, which is exactly what marks them as sheets.
        volume = bm.calc_volume(signed=False)
        open_edges = sum(1 for e in bm.edges if len(e.link_faces) == 1)
        non_manifold = sum(1 for e in bm.edges if len(e.link_faces) > 2)
        tris = sum(len(f.verts) - 2 for f in bm.faces)

        xs = [v.co.x for v in bm.verts]
        ys = [v.co.y for v in bm.verts]
        zs = [v.co.z for v in bm.verts]

        # 2*V/A. For a CLOSED thin shell of thickness t the volume is (area/2)*t,
        # so this returns t. For an OPEN shell it returns nothing meaningful --
        # measured on a real character it reports 208 mm for a forearm. Its one
        # reliable statement is at zero: a single-layer sheet (cloak, tabard,
        # hair plane) encloses no volume at all, and that is the one distinction
        # this step actually needs. Named "bulk", not "thickness", so nobody
        # downstream mistakes the nonzero values for wall thickness.
        bulk = (2.0 * volume / area) if area > 0.0 else 0.0

        materials = [m for m in obj.data.materials if m is not None]
        return {
            "name": obj.name,
            "tris": tris,
            "area_mm2": area * BU_TO_MM * BU_TO_MM,
            "volume_mm3": volume * BU_TO_MM ** 3,
            "bulk_mm": bulk * BU_TO_MM,
            "open_edges": open_edges,
            "edges": len(bm.edges),
            "non_manifold": non_manifold,
            "size_mm": (
                (max(xs) - min(xs)) * BU_TO_MM,
                (max(ys) - min(ys)) * BU_TO_MM,
                (max(zs) - min(zs)) * BU_TO_MM,
            ),
            "x_mm": (min(xs) * BU_TO_MM, max(xs) * BU_TO_MM),
            "y_mm": (min(ys) * BU_TO_MM, max(ys) * BU_TO_MM),
            "z_mm": (min(zs) * BU_TO_MM, max(zs) * BU_TO_MM),
            "hidden": bool(obj.hide_viewport),
            "effect_plane": any(m.get("wmv_effect_plane") for m in materials),
            "two_sided": any(m.get("wmv_two_sided") for m in materials),
            "has_stamp": any("wmv_blend_mode" in m for m in materials),
        }
    finally:
        bm.free()


def _classify_ambiguous(m, wall_mm):
    """Decide what to do with a part the unambiguous rules could not place --
    one with open boundary edges, or a measured thickness somewhere between
    "sheet" and "already thicker than the wall we would print anyway".

    m is one dict from _print_measure() (all lengths in millimetres at the
    CURRENT scene scale, not at print scale); wall_mm is the wall thickness the
    pipeline would solidify to. Useful keys: bulk_mm, open_edges, edges,
    non_manifold, tris, area_mm2, volume_mm3, size_mm, two_sided.

    Return one of:
        "nullflaeche"  -- treat as a zero-thickness sheet, give it thickness
        "geschlossen"  -- treat as solid, leave it alone
        None           -- undecided; reported as "unklar" with its numbers,
                          which is the honest answer until the numbers are in

    What the first real measurement (dkhumanredarmor, 23 parts) established:
      * NOT ONE part is closed -- every single one has open boundary edges, so
        "open_edges == 0" cannot be the test for "solid". The seams at wrist,
        ankle and neck are normal WoW geometry, not damage.
      * bulk_mm separates cleanly at the bottom end and nowhere else: cloak and
        tabard sit at exactly 0.000, every other part is above 0.28, and the
        large values (up to 208) are artefacts of measuring an open shell.
      * The boundary ratio (open_edges / edges) ranges from about 5% on solid
        armour to over 30% on the thin plates -- the one continuous signal
        available here that is not an artefact.

    The default answers "geschlossen", on the asymmetry of the two mistakes:

      * Thicken a solid by mistake and the blade goes blunt, the buckle gap
        closes, and the result still slices cleanly and prints. The damage is
        invisible until the figure is in your hand.
      * Leave a thin part alone by mistake and it prints thin, or the slicer
        drops a wall it cannot fit. That is loud, and it is visible in the
        slicer preview before any filament is spent.

    Measured on both available characters this is also the factually right
    answer: every ambiguous part was solid armour whose open edges came from
    geoset seams, and the two genuine sheets both scored exactly 0.000 and were
    caught before reaching this function. Flip AMBIGUOUS_IS_SOLID to change it
    in one line once a real print says otherwise.
    """
    if m["edges"] and m["open_edges"] / m["edges"] > OPEN_SHEET_RATIO:
        return "nullflaeche"
    return "geschlossen" if AMBIGUOUS_IS_SOLID else None


def _print_role(m, wall_mm):
    """Where a measured part belongs in the print pipeline.

    Only the two ends are decided here, and both by measurement: a part with no
    volume worth the name is a sheet, a closed part thicker than the wall is a
    solid. Everything between goes to _classify_ambiguous(), and whatever that
    declines to answer is reported as "unklar" rather than quietly sorted into
    whichever bucket happened to be easier to code."""
    if m["effect_plane"]:
        return "effekt"
    if m["bulk_mm"] < SHEET_MM:
        return "nullflaeche"
    if m["open_edges"] == 0 and m["non_manifold"] == 0 and m["bulk_mm"] >= wall_mm:
        return "geschlossen"
    return _classify_ambiguous(m, wall_mm) or "unklar"


# ---------------------------------------------------------------------------
# The profile: six numbers, and no process anywhere in the code.
#
# The temptation is a "printer type" dropdown -- FDM, resin, SLS -- with an
# if-branch behind each. It is the wrong shape twice over: it hardcodes today's
# three processes, and it makes the tool useless the moment a fourth appears.
#
# Every process peculiarity is therefore a NUMBER that could equally describe a
# process nobody has built yet:
#
#   build volume x/y/z    does it fit, does it need cutting
#   smallest feature      nozzle diameter OR pixel pitch -- the same role
#   minimum wall          what Solidify aims for; 0 derives it as 3x feature
#   max overhang          90 means overhangs are irrelevant (SLS, MJF)
#   tilt                  0 or 30, instead of asking which process this is
#   escape hole           0 means none; covers resin drainage AND powder escape
#
# The rule that keeps it honest, from DRUCK-IDEEN.md: measure always, judge only
# in the display. No geometry step reads the build volume or the overhang angle;
# they only change what the report says.
# ---------------------------------------------------------------------------

# Shipped defaults, not a recommendation: the most common desktop FDM machine
# (256 mm cube, 0.4 nozzle). Someone printing resin or ordering from a service
# overwrites all six and nothing in the code notices the difference.
PROFILE_DEFAULTS = {
    "build_x_mm": 256.0,
    "build_y_mm": 256.0,
    "build_z_mm": 256.0,
    "feature_mm": 0.4,
    "wall_mm": 0.0,
    "overhang_deg": 45.0,
    "tilt_deg": 0.0,
    "escape_hole_mm": 0.0,
}

PROFILE_PROPERTIES = {
    "build_x_mm": "wmv_print_build_x_mm",
    "build_y_mm": "wmv_print_build_y_mm",
    "build_z_mm": "wmv_print_build_z_mm",
    "feature_mm": "wmv_print_feature_mm",
    "wall_mm": "wmv_print_wall_mm",
    "overhang_deg": "wmv_print_overhang_deg",
    "tilt_deg": "wmv_print_tilt_deg",
    "escape_hole_mm": "wmv_print_escape_hole_mm",
}


def _print_profile(scene):
    """Read the six profile fields plus the target height into a plain dict.

    A dict rather than the scene, so every consumer below stays pure and can be
    checked without Blender -- and so a missing property falls back to the
    default instead of raising. That fallback is the first half of the test in
    DRUCK-IDEEN.md: delete the profile, does the tool still do something
    sensible?"""
    profile = dict(PROFILE_DEFAULTS)
    for key, prop in PROFILE_PROPERTIES.items():
        value = getattr(scene, prop, None)
        if value is not None:
            profile[key] = float(value)
    profile["height_mm"] = float(getattr(scene, "wmv_print_height_mm", 200.0))
    return profile


def _print_wall_mm(profile):
    """The wall thickness to aim for. Zero means "derive it": three extrusion
    widths is the thinnest wall that still prints as a wall rather than as a
    single wobbling bead, and it scales with the feature size, so it is right
    for a 0.2 nozzle and for a resin printer's pixel pitch alike."""
    wall = profile.get("wall_mm", 0.0)
    if wall > 0.0:
        return wall
    return 3.0 * profile.get("feature_mm", PROFILE_DEFAULTS["feature_mm"])


def _print_fit(printed_mm, profile):
    """Compare the printed bounding box against the build volume.

    Returns (fits, lines). The figure may be turned freely on the plate, so the
    two horizontal axes are matched largest-to-largest rather than X-to-X --
    anything else reports a false failure for a figure that only needs rotating
    by ninety degrees."""
    footprint = sorted(printed_mm[:2], reverse=True)
    plate = sorted((profile["build_x_mm"], profile["build_y_mm"]), reverse=True)
    height_fits = printed_mm[2] <= profile["build_z_mm"]
    plate_fits = all(f <= p for f, p in zip(footprint, plate))

    lines = []
    if height_fits and plate_fits:
        lines.append("Passt in den Bauraum (%.0f x %.0f x %.0f mm)."
                     % (profile["build_x_mm"], profile["build_y_mm"],
                        profile["build_z_mm"]))
    else:
        what = []
        if not height_fits:
            what.append("Hoehe %.0f > %.0f mm" % (printed_mm[2], profile["build_z_mm"]))
        if not plate_fits:
            what.append("Grundflaeche %.0f x %.0f > %.0f x %.0f mm"
                        % (footprint[0], footprint[1], plate[0], plate[1]))
        lines.append("Passt NICHT in den Bauraum: %s." % ", ".join(what))
        if not height_fits and profile["build_z_mm"] > 0:
            # Ceiling, not floor-plus-one: a figure exactly twice the build
            # height needs two parts, not three.
            whole = int(printed_mm[2] / profile["build_z_mm"])
            parts = whole + (1 if printed_mm[2] > whole * profile["build_z_mm"] else 0)
            lines.append("  Mindestens %d Teile -- \"Zerteilen\" schneidet an der "
                         "schmalsten Stelle." % parts)
    return (height_fits and plate_fits), lines


def _print_process_notes(profile):
    """What the three process-shaped numbers mean for this figure. No branch on
    a process name anywhere -- each line is triggered by its own number."""
    notes = []
    if profile["overhang_deg"] >= 90.0:
        notes.append("Ueberhaenge sind laut Profil gleichgueltig (%.0f Grad) -- "
                     "keine Stuetzen, keine Ausrichtung noetig."
                     % profile["overhang_deg"])
    else:
        notes.append("Ueberhaenge ab %.0f Grad brauchen Stuetzen. Die Standpose "
                     "druckt sich besser als die T-Pose; das Kippen und die "
                     "Stuetzen selbst gehoeren in den Slicer."
                     % profile["overhang_deg"])
    if profile["tilt_deg"] > 0.0:
        notes.append("Profil schlaegt %.0f Grad Kippung vor -- im Slicer "
                     "einstellen, nicht hier." % profile["tilt_deg"])
    if profile["escape_hole_mm"] > 0.0:
        notes.append("Profil verlangt Austrittsloecher ab %.1f mm (Harzablauf "
                     "oder Pulveraustritt). Diese Kette druckt massiv und bohrt "
                     "keine -- beim Hohlen im Slicer selbst setzen."
                     % profile["escape_hole_mm"])
    return notes


def _print_report(measurements, profile):
    """Build the report as a list of lines. Pure -- no scene access -- so the
    arithmetic can be checked against a fixture without a running Blender."""
    # Filled out against the defaults first, so a profile that is missing
    # fields -- or is only {"height_mm": ...} -- still produces a full report
    # rather than a KeyError. That is the "delete the profile" half of the test
    # rule in DRUCK-IDEEN.md, and it has to hold here, not only in the UI.
    profile = dict(PROFILE_DEFAULTS, **profile)
    live = [m for m in measurements if not m["hidden"]]
    wall_mm = _print_wall_mm(profile)
    target_mm = profile.get("height_mm", 200.0)
    feature_mm = profile["feature_mm"]

    lines = ["WMV-Druckbericht", "=" * 64, ""]
    if not live:
        lines.append("Keine sichtbare Geometrie gefunden -- nichts zu messen.")
        return lines, {}

    height_mm = max(m["z_mm"][1] for m in live) - min(m["z_mm"][0] for m in live)
    if height_mm <= 0.0:
        lines.append("Die sichtbare Geometrie hat keine Hoehe -- nichts zu messen.")
        return lines, {}

    # The scale the pipeline would have to apply, derived from the measured
    # height and never hardcoded: a gnome and a tauren are a factor of two apart.
    scale = target_mm / height_mm

    roles = {}
    for m in live:
        roles.setdefault(_print_role(m, wall_mm), []).append(m)

    lines += [
        "Gemessene Figur:  %.1f mm hoch (%.0f x %.0f mm Grundflaeche)" % (
            height_mm,
            max(m["x_mm"][1] for m in live) - min(m["x_mm"][0] for m in live),
            max(m["y_mm"][1] for m in live) - min(m["y_mm"][0] for m in live)),
        "Zielhoehe:        %.0f mm  ->  Massstab %.4f (etwa 1:%.0f)" % (
            target_mm, scale, height_mm / target_mm),
        "Kleinstes Merkmal:%.2f mm  ->  Wandstaerke %.2f mm%s" % (
            feature_mm, wall_mm,
            " (3 x Merkmal)" if profile.get("wall_mm", 0.0) <= 0.0
            else " (aus dem Profil)"),
        "Gedruckt:         %.0f x %.0f x %.0f mm" % (
            (max(m["x_mm"][1] for m in live)
             - min(m["x_mm"][0] for m in live)) * scale,
            (max(m["y_mm"][1] for m in live)
             - min(m["y_mm"][0] for m in live)) * scale,
            target_mm),
        "Dreiecke gesamt:  %d" % sum(m["tris"] for m in live),
        "",
        "Am UNSKALIERTEN Modell einzustellen -- Solidify und Remesh rechnen in",
        "lokalen Koordinaten, das sind also die Zahlen, die dort hineingehoeren:",
        "  Solidify-Dicke  %.6f BU" % (wall_mm / scale / BU_TO_MM),
        "  Voxelgroesse    %.6f BU   (= %.2f mm am gedruckten Teil)" % (
            VOXEL_OF_FEATURE * feature_mm / scale / BU_TO_MM,
            VOXEL_OF_FEATURE * feature_mm),
        "",
        "2V/A ist nur bei 0 aussagekraeftig -- dort liegt eine einlagige Flaeche",
        "ohne Volumen. Groessere Werte sind bei offenen Schalen Messartefakte,",
        "keine Wandstaerken.",
        "",
        "-" * 64,
    ]

    labels = (
        ("effekt", "EFFEKTFLAECHEN -- wuerden geloescht"),
        ("nullflaeche", "NULLFLAECHEN -- brauchen Dicke"),
        ("unklar", "UNKLAR -- hier entscheidet noch keine Regel"),
        ("geschlossen", "GESCHLOSSEN -- unveraendert uebernehmen"),
    )
    for role, label in labels:
        group = roles.get(role)
        if not group:
            continue
        lines += ["", "%s  (%d)" % (label, len(group)),
                  "  %-34s %8s %10s %8s %7s" % (
                      "Teil", "Dreiecke", "2V/A (mm)", "Randkanten", "Anteil")]
        for m in sorted(group, key=lambda x: -x["tris"]):
            ratio = (100.0 * m["open_edges"] / m["edges"]) if m["edges"] else 0.0
            lines.append(
                "  %-34s %8d %10.3f %8d %6.1f%%%s" % (
                    m["name"][:34], m["tris"], m["bulk_mm"], m["open_edges"],
                    ratio, "  [twoSided]" if m["two_sided"] else ""))

    notes = []
    if any(not m["has_stamp"] for m in live):
        notes.append(
            "Mindestens ein Objekt hat keine WMV-Materialstempel -- ohne "
            ".wmvmat.json neben der FBX sind Effektflaechen nicht erkennbar.")
    if not roles.get("geschlossen"):
        notes.append(
            "Kein einziges Teil ist geschlossen -- jedes hat offene Randkanten. "
            "\"Keine offenen Kanten\" taugt damit nicht als Test fuer \"massiv\".")
    if roles.get("unklar"):
        notes.append(
            "%d Teile sind unentschieden. Genau dafuer ist dieser Bericht da: "
            "die Regel gehoert an diese Zahlen angepasst, nicht umgekehrt."
            % len(roles["unklar"]))
    printed_mm = (
        (max(m["x_mm"][1] for m in live) - min(m["x_mm"][0] for m in live)) * scale,
        (max(m["y_mm"][1] for m in live) - min(m["y_mm"][0] for m in live)) * scale,
        target_mm)
    _, fit_lines = _print_fit(printed_mm, profile)
    notes.extend(fit_lines)
    notes.extend(_print_process_notes(profile))
    # Geoset 0 ends at the wrist and the ankle; hand (401) and foot (501) are
    # separate, unwelded shells. That seam is real geometry, not damage -- said
    # here so a later manifold check is not mistaken for a broken export.
    notes.append(
        "Handgelenk, Knoechel und Halsansatz sind im WMV-Export offene Naehte "
        "(getrennte Geosets). Offene Kanten dort sind normal, kein Schaden.")

    lines += ["", "-" * 64, "", "HINWEISE"]
    lines += ["  * " + n for n in notes]
    return lines, roles


class WMV_OT_print_profile_save(bpy.types.Operator, ExportHelper):
    """Write the six profile fields to a JSON file"""

    bl_idname = "wmv.print_profile_save"
    bl_label = "Druckprofil speichern"
    bl_options = {"REGISTER"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        profile = _print_profile(context.scene)
        profile.pop("height_mm", None)  # the figure's size, not the machine's
        try:
            with open(self.filepath, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "profile": profile}, handle,
                          indent=2, sort_keys=True)
        except OSError as error:
            self.report({"ERROR"}, "Nicht schreibbar: %s" % error)
            return {"CANCELLED"}
        self.report({"INFO"}, "Profil gespeichert: %s"
                    % os.path.basename(self.filepath))
        return {"FINISHED"}


class WMV_OT_print_profile_load(bpy.types.Operator, ImportHelper):
    """Read the six profile fields back from a JSON file"""

    bl_idname = "wmv.print_profile_load"
    bl_label = "Druckprofil laden"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        try:
            with open(self.filepath, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, "Nicht lesbar: %s" % error)
            return {"CANCELLED"}

        stored = data.get("profile")
        if not isinstance(stored, dict):
            self.report({"ERROR"},
                        "Keine Profildaten in der Datei -- erwartet wird "
                        "{\"version\": 1, \"profile\": {...}}.")
            return {"CANCELLED"}

        # Unknown keys are ignored rather than rejected: a profile written by a
        # later version with a seventh field must still load here, minus that
        # field, instead of failing outright.
        applied, ignored = 0, []
        for key, value in stored.items():
            prop = PROFILE_PROPERTIES.get(key)
            if prop is None:
                ignored.append(key)
                continue
            try:
                setattr(context.scene, prop, float(value))
                applied += 1
            except (TypeError, ValueError):
                ignored.append(key)

        if not applied:
            self.report({"ERROR"}, "Keines der Felder war verwertbar.")
            return {"CANCELLED"}
        self.report({"INFO"}, "%d Felder geladen%s" % (
            applied, ", %d ignoriert (%s)" % (len(ignored), ", ".join(ignored[:3]))
            if ignored else ""))
        return {"FINISHED"}


class WMV_OT_print_profile_reset(bpy.types.Operator):
    """Put the six profile fields back to their shipped defaults"""

    bl_idname = "wmv.print_profile_reset"
    bl_label = "Druckprofil zuruecksetzen"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        # The first half of the test rule in DRUCK-IDEEN.md: throw the profile
        # away and see whether the tool still does something sensible. It has to
        # -- every consumer reads through _print_profile(), which falls back to
        # PROFILE_DEFAULTS field by field.
        for key, prop in PROFILE_PROPERTIES.items():
            setattr(context.scene, prop, PROFILE_DEFAULTS[key])
        self.report({"INFO"}, "Profil auf die Vorgabewerte zurueckgesetzt "
                              "(%.0f mm Bauraum, %.2f mm kleinstes Merkmal)"
                    % (PROFILE_DEFAULTS["build_z_mm"],
                       PROFILE_DEFAULTS["feature_mm"]))
        return {"FINISHED"}


class WMV_OT_print_check(bpy.types.Operator):
    """Measure the scene for 3D printing -- changes nothing, writes a report"""

    bl_idname = "wmv.print_check"
    bl_label = "Druckzustand pruefen"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        depsgraph = context.evaluated_depsgraph_get()

        measurements = []
        for obj in scene.objects:
            if obj.type != "MESH":
                continue
            measured = _print_measure(obj, depsgraph)
            if measured is not None:
                measurements.append(measured)

        if not measurements:
            self.report({"ERROR"},
                        "Keine Mesh-Objekte in der Szene. Erst einen "
                        "WMV-Export importieren.")
            return {"CANCELLED"}

        lines, roles = _print_report(measurements, _print_profile(scene))

        # Written to a text datablock rather than only reported: the report is
        # a table, and Blender's status line shows one line.
        text = bpy.data.texts.get("WMV-Druckbericht")
        if text is None:
            text = bpy.data.texts.new("WMV-Druckbericht")
        text.clear()
        text.write("\n".join(lines) + "\n")
        for line in lines:
            print(line)

        self.report(
            {"INFO"},
            "%d Objekte gemessen, %d unklar -- Bericht im Texteditor unter "
            "\"WMV-Druckbericht\"" % (len(measurements),
                                      len(roles.get("unklar", []))),
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# The print pipeline itself: five steps, each its own operator.
#
# Deliberately not one button. Every step changes the scene in a way the next
# one depends on, and when the result is wrong the only useful question is WHICH
# step made it wrong -- a single operator answers that with silence. Each step
# runs on its own, reports what it touched, and refuses loudly when its
# precondition is missing. "Alles ausfuehren" runs them in order and stops at
# the first refusal, naming it.
#
# The order is fixed and two of the four orderings matter:
#   * strip before everything -- effect planes would otherwise be frozen,
#     thickened and remeshed along with the figure.
#   * scale applied before Solidify -- the modifier reads local vertex
#     coordinates, so an unapplied scale gives a cloak a different wall
#     thickness on each side. Measured: without apply 0.4 instead of 0.1.
#     (Whether Solidify is set before or after the apply does NOT matter --
#     both orderings measure identically. Only that the apply happens.)
# ---------------------------------------------------------------------------


def _print_meshes(context, include_hidden=False):
    """Mesh objects the pipeline acts on, in a stable order."""
    return [o for o in context.scene.objects
            if o.type == "MESH" and (include_hidden or not o.hide_viewport)]


def _print_geometry(context, include_hidden=False):
    """Measure every pipeline mesh. Returns (measurements, height_mm)."""
    depsgraph = context.evaluated_depsgraph_get()
    measurements = []
    for obj in _print_meshes(context, include_hidden):
        measured = _print_measure(obj, depsgraph)
        if measured is not None:
            measurements.append(measured)
    if not measurements:
        return [], 0.0
    height = (max(m["z_mm"][1] for m in measurements)
              - min(m["z_mm"][0] for m in measurements))
    return measurements, height


def _print_scale(context):
    """The factor between scene millimetres and printed millimetres, measured
    from the figure rather than assumed. Returns (scale, height_mm) with
    scale == 0.0 when there is nothing to measure."""
    _, height_mm = _print_geometry(context)
    if height_mm <= 0.0:
        return 0.0, 0.0
    return context.scene.wmv_print_height_mm / height_mm, height_mm


def _print_apply_modifier(context, obj, name):
    """Apply one modifier on one object, whatever the current selection is.

    Mesh data shared between objects cannot take an applied modifier, and a
    hidden object refuses outright -- both happen on a real WMV import, so both
    are handled here rather than at every call site."""
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    was_hidden = obj.hide_viewport
    obj.hide_viewport = False
    try:
        with context.temp_override(object=obj, active_object=obj,
                                   selected_objects=[obj],
                                   selected_editable_objects=[obj]):
            bpy.ops.object.modifier_apply(modifier=name)
    finally:
        obj.hide_viewport = was_hidden


def _stl_bounds(path, contact_mm=0.3):
    """Read back a binary STL and measure it. Returns a dict with

        triangles  facet count
        size       (dx, dy, dz) bounding box
        floor      z of the lowest point -- 0.0 when it sits on the plate
        contact    (cx, cy, facets) footprint within contact_mm of the bottom

    in whatever unit the file carries -- which is the whole point: STL has no
    unit field, so the only way to know the figure came out at the requested
    size is to measure the file that was actually written.

    The contact patch is measured for a reason a slicer only tells you about
    after it has failed: a WoW standing pose can touch the plate on a single
    toe. PrusaSlicer answers that with "there is an object with no extrusions in
    the first layer" and writes nothing. Measured here, it is a sentence before
    the export instead of a mystery after it.

    Returns None if the file is not a binary STL."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(84)
            if len(header) < 84:
                return None
            count = struct.unpack("<I", header[80:84])[0]
            if count == 0:
                return {"triangles": 0, "size": (0.0, 0.0, 0.0),
                        "contact": (0.0, 0.0, 0)}
            lo = [float("inf")] * 3
            hi = [float("-inf")] * 3
            facets = []
            for _ in range(count):
                chunk = handle.read(50)
                if len(chunk) < 50:
                    return None
                values = struct.unpack("<12f", chunk[:48])
                corners = [values[3 * c:3 * c + 3] for c in range(1, 4)]
                facets.append(corners)
                for corner in corners:
                    for axis in range(3):
                        lo[axis] = min(lo[axis], corner[axis])
                        hi[axis] = max(hi[axis], corner[axis])

        limit = lo[2] + contact_mm
        xs, ys, touching = [], [], 0
        for corners in facets:
            if min(c[2] for c in corners) <= limit:
                touching += 1
                xs.extend(c[0] for c in corners)
                ys.extend(c[1] for c in corners)
        contact = ((max(xs) - min(xs), max(ys) - min(ys), touching)
                   if xs else (0.0, 0.0, 0))
        return {"triangles": count,
                "size": tuple(hi[i] - lo[i] for i in range(3)),
                # Where the bottom actually sits. Zero means the figure stands
                # on the plate; anything else means the export forgot to place
                # it and the slicer will move it silently.
                "floor": lo[2],
                "contact": contact}
    except (OSError, struct.error, MemoryError):
        return None


class WMV_OT_print_strip(bpy.types.Operator):
    """Delete the frozen particle/effect billboards -- step 1 of 6"""

    bl_idname = "wmv.print_strip"
    bl_label = "1 - Effektflaechen entfernen"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        meshes = _print_meshes(context, include_hidden=True)
        if not meshes:
            self.report({"ERROR"}, "Keine Mesh-Objekte in der Szene.")
            return {"CANCELLED"}

        stamped = 0
        doomed, mixed = [], []
        for obj in meshes:
            mats = [m for m in obj.data.materials if m is not None]
            if any("wmv_blend_mode" in m for m in mats):
                stamped += 1
            flags = [bool(m.get("wmv_effect_plane")) for m in mats]
            if flags and all(flags):
                doomed.append(obj.name)
            elif any(flags):
                # Only reachable when the import ran without separate_materials;
                # deleting the whole object would take real armour with it.
                mixed.append(obj.name)

        if stamped == 0:
            self.report({"ERROR"},
                        "Keine WMV-Materialstempel gefunden. Ohne .wmvmat.json "
                        "neben der FBX sind Effektflaechen nicht erkennbar -- "
                        "neu aus dem Model Viewer exportieren.")
            return {"CANCELLED"}

        # By name, not by reference: removing invalidates the other pointers.
        for name in doomed:
            obj = bpy.data.objects.get(name)
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)

        if mixed:
            self.report({"WARNING"},
                        "%d Objekte mischen Effekt- und echte Materialien und "
                        "bleiben unangetastet: %s" % (len(mixed),
                                                      ", ".join(mixed[:3])))
        self.report({"INFO"}, "%d Effektflaechen entfernt, %d Objekte bleiben"
                    % (len(doomed), len(_print_meshes(context, True))))
        return {"FINISHED"}


class WMV_OT_print_freeze(bpy.types.Operator):
    """Apply the pose and the scale, then drop the skeleton -- step 2 of 6"""

    bl_idname = "wmv.print_freeze"
    bl_label = "2 - Pose und Massstab einfrieren"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        meshes = _print_meshes(context, include_hidden=True)
        if not meshes:
            self.report({"ERROR"}, "Keine Mesh-Objekte in der Szene.")
            return {"CANCELLED"}

        applied = 0
        for obj in meshes:
            for mod in [m for m in obj.modifiers if m.type == "ARMATURE"]:
                _print_apply_modifier(context, obj, mod.name)
                applied += 1
            # Unparent by hand rather than via parent_clear: no selection to
            # arrange, and the world transform is preserved by construction.
            if obj.parent is not None:
                world = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = world

        # Now that nothing inherits a transform, the scale can be applied per
        # object -- which is what Solidify and Remesh need, both reading local
        # vertex coordinates.
        for obj in meshes:
            was_hidden = obj.hide_viewport
            obj.hide_viewport = False
            try:
                with context.temp_override(object=obj, active_object=obj,
                                           selected_objects=[obj],
                                           selected_editable_objects=[obj]):
                    bpy.ops.object.transform_apply(location=False,
                                                   rotation=False, scale=True)
            finally:
                obj.hide_viewport = was_hidden

        # transform_apply reports FINISHED even when it changed nothing, so the
        # result is checked instead of trusted.
        unscaled = [o.name for o in meshes
                    if any(abs(c - 1.0) > 1e-4 for c in o.matrix_world.to_scale())]

        for armature in [o for o in context.scene.objects if o.type == "ARMATURE"]:
            bpy.data.objects.remove(armature, do_unlink=True)

        if unscaled:
            self.report({"ERROR"},
                        "%d Objekte tragen weiter eine Skalierung (%s). Solidify "
                        "wuerde dort ungleiche Wandstaerken erzeugen."
                        % (len(unscaled), ", ".join(unscaled[:3])))
            return {"CANCELLED"}
        self.report({"INFO"},
                    "%d Armature-Modifier angewendet, Massstab auf %d Objekten "
                    "angewendet, Skelett entfernt" % (applied, len(meshes)))
        return {"FINISHED"}


class WMV_OT_print_thicken(bpy.types.Operator):
    """Give the zero-thickness sheets a printable wall -- step 3 of 6"""

    bl_idname = "wmv.print_thicken"
    bl_label = "3 - Dicke geben"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        measurements, height_mm = _print_geometry(context)
        if height_mm <= 0.0:
            self.report({"ERROR"}, "Nichts zu messen.")
            return {"CANCELLED"}

        profile = _print_profile(scene)
        scale = profile["height_mm"] / height_mm
        wall_mm = _print_wall_mm(profile)
        # Scene millimetres -> Blender units. The wall is specified at PRINT
        # size, so it has to be divided back out by the scale the figure will be
        # shrunk by; at 1:10 a 1.2 mm printed wall is 12 mm of scene geometry.
        thickness_bu = wall_mm / scale / BU_TO_MM

        thickened, skipped = [], 0
        for m in measurements:
            if _print_role(m, wall_mm) != "nullflaeche":
                skipped += 1
                continue
            obj = bpy.data.objects.get(m["name"])
            if obj is None:
                continue
            mod = obj.modifiers.new(name="WMV Druck Dicke", type="SOLIDIFY")
            mod.thickness = thickness_bu
            # Centred, so a cloak grows equally to both sides and keeps sitting
            # where the silhouette says it does.
            mod.offset = 0.0
            mod.use_even_offset = True
            mod.use_rim = True
            mod.use_rim_only = False
            _print_apply_modifier(context, obj, mod.name)
            thickened.append(obj.name)

        if not thickened:
            self.report({"WARNING"},
                        "Kein Teil brauchte Dicke. Entweder ist die Figur schon "
                        "massiv, oder Schritt 1 hat die Flaechen bereits "
                        "entfernt -- der Bericht aus \"Druckzustand pruefen\" "
                        "sagt welches.")
            return {"FINISHED"}
        self.report({"INFO"},
                    "%d Teile auf %.2f mm gedruckte Wandstaerke verdickt "
                    "(%.6f BU), %d unveraendert"
                    % (len(thickened), wall_mm, thickness_bu, skipped))
        return {"FINISHED"}


class WMV_OT_print_unify(bpy.types.Operator):
    """Join everything into one body, optionally voxel-remeshed -- step 4 of 6"""

    bl_idname = "wmv.print_unify"
    bl_label = "4 - Vereinigen"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        meshes = _print_meshes(context)
        if not meshes:
            self.report({"ERROR"}, "Keine sichtbaren Mesh-Objekte.")
            return {"CANCELLED"}

        measurements, height_mm = _print_geometry(context)
        if height_mm <= 0.0:
            self.report({"ERROR"}, "Nichts zu messen.")
            return {"CANCELLED"}
        scale = scene.wmv_print_height_mm / height_mm
        # Measured here, before the join: object.dimensions is derived from the
        # bounding box and does not refresh until the depsgraph does, so reading
        # it straight after join() returns the size of whatever the first object
        # was -- which silently defeated this guard.
        longest_bu = max(
            max(m["x_mm"][1] for m in measurements) - min(m["x_mm"][0] for m in measurements),
            max(m["y_mm"][1] for m in measurements) - min(m["y_mm"][0] for m in measurements),
            height_mm) / BU_TO_MM

        target = meshes[0]
        if len(meshes) > 1:
            with context.temp_override(object=target, active_object=target,
                                       selected_objects=meshes,
                                       selected_editable_objects=meshes):
                bpy.ops.object.join()
        target.name = "WMV_Druck"

        if not scene.wmv_print_remesh:
            self.report({"INFO"},
                        "%d Objekte zu einem verbunden, kein Remesh -- die "
                        "Schalen ueberlappen sich weiterhin, das vereinigt erst "
                        "der Slicer." % len(meshes))
            return {"FINISHED"}

        feature_mm = _print_profile(scene)["feature_mm"]
        voxel_bu = VOXEL_OF_FEATURE * feature_mm / scale / BU_TO_MM
        # OpenVDB is sparse, so the real cost tracks the surface rather than the
        # volume -- but a voxel far too small still runs for minutes and looks
        # like a hang. Refuse instead, with the number that caused it.
        span = longest_bu / voxel_bu if voxel_bu > 0 else 0
        if span > 4000:
            self.report({"ERROR"},
                        "Voxelgroesse %.6f BU ergaebe %.0f Voxel ueber die "
                        "laengste Kante. Groeberes kleinstes Merkmal waehlen "
                        "oder Remesh abschalten." % (voxel_bu, span))
            return {"CANCELLED"}

        mod = target.modifiers.new(name="WMV Druck Remesh", type="REMESH")
        mod.mode = "VOXEL"
        mod.voxel_size = voxel_bu
        mod.use_smooth_shade = False
        _print_apply_modifier(context, target, mod.name)

        # Sweep up crumbs: thin-shell solidify can leave scraps that the remesh
        # turns into tiny disconnected islands, floating beside the figure and
        # printing as loose grains in the supports. Split, drop everything
        # smaller than CRUMB_MM at print size, rejoin. Sizes are measured from
        # the mesh data, NOT object.dimensions -- which is stale right after
        # separate() and has produced wrong numbers here twice before.
        with context.temp_override(object=target, active_object=target,
                                   selected_objects=[target],
                                   selected_editable_objects=[target]):
            bpy.ops.mesh.separate(type="LOOSE")
        pieces, crumbs = [], []
        for piece in _print_meshes(context):
            xs = [v.co.x for v in piece.data.vertices]
            ys = [v.co.y for v in piece.data.vertices]
            zs = [v.co.z for v in piece.data.vertices]
            size_mm = max(max(xs) - min(xs), max(ys) - min(ys),
                          max(zs) - min(zs)) * BU_TO_MM * scale if xs else 0.0
            (crumbs if size_mm < CRUMB_MM else pieces).append(piece)
        for crumb in crumbs:
            bpy.data.objects.remove(crumb, do_unlink=True)
        if not pieces:
            self.report({"ERROR"}, "Nach dem Aufraeumen ist nichts uebrig -- "
                                   "das sollte nicht passieren.")
            return {"CANCELLED"}
        target = pieces[0]
        if len(pieces) > 1:
            with context.temp_override(object=target, active_object=target,
                                       selected_objects=pieces,
                                       selected_editable_objects=pieces):
                bpy.ops.object.join()
        target.name = "WMV_Druck"
        swept = (", %d Kruemel entfernt" % len(crumbs)) if crumbs else ""

        self.report({"INFO"},
                    "%d Objekte vereinigt und mit %.6f BU Voxel neu vernetzt "
                    "(= %.2f mm gedruckt) -- jede scharfe Kante verliert dabei "
                    "etwa eine Voxelbreite%s"
                    % (len(meshes), voxel_bu, VOXEL_OF_FEATURE * feature_mm,
                       swept))
        return {"FINISHED"}


class WMV_OT_print_decimate(bpy.types.Operator):
    """Bring the triangle count down to something a slicer enjoys -- step 5"""

    bl_idname = "wmv.print_decimate"
    bl_label = "5 - Dreiecke reduzieren"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        limit = context.scene.wmv_print_max_tris
        meshes = _print_meshes(context)
        if not meshes:
            self.report({"ERROR"}, "Keine sichtbaren Mesh-Objekte.")
            return {"CANCELLED"}
        if limit <= 0:
            self.report({"INFO"}, "Reduktion abgeschaltet (Grenze 0).")
            return {"FINISHED"}

        reduced = 0
        for obj in meshes:
            # Triangles, not polygons: the remesh leaves quads, and the limit
            # every slicer and print service states is in triangles.
            tris = sum(len(poly.vertices) - 2 for poly in obj.data.polygons)
            if tris <= limit:
                continue
            mod = obj.modifiers.new(name="WMV Druck Reduktion", type="DECIMATE")
            mod.decimate_type = "COLLAPSE"
            mod.ratio = float(limit) / float(tris)
            # Collapse on a voxel-remeshed mesh is well behaved -- the input is
            # uniform and closed, which is the case the algorithm is good at.
            # It is applied after the remesh for exactly that reason.
            _print_apply_modifier(context, obj, mod.name)
            reduced += 1

        after = sum(sum(len(p.vertices) - 2 for p in o.data.polygons)
                    for o in _print_meshes(context))
        if not reduced:
            self.report({"INFO"},
                        "%d Dreiecke, unter der Grenze von %d -- nichts zu tun."
                        % (after, limit))
            return {"FINISHED"}
        self.report({"INFO"}, "Auf %d Dreiecke reduziert (Grenze %d)"
                    % (after, limit))
        return {"FINISHED"}


class WMV_OT_print_export(bpy.types.Operator, ExportHelper):
    """Write the STL at the target height and measure the file back -- step 6"""

    bl_idname = "wmv.print_export"
    bl_label = "6 - STL schreiben"
    bl_options = {"REGISTER"}

    filename_ext = ".stl"
    filter_glob: StringProperty(default="*.stl", options={"HIDDEN"})

    def invoke(self, context, event):
        last = _read_last_export()
        if last:
            self.filepath = os.path.splitext(last)[0] + ".stl"
        return super().invoke(context, event)

    def execute(self, context):
        meshes = _print_meshes(context)
        if not meshes:
            self.report({"ERROR"}, "Keine sichtbaren Mesh-Objekte.")
            return {"CANCELLED"}

        measurements, height_mm = _print_geometry(context)
        if height_mm <= 0.0:
            self.report({"ERROR"}, "Nichts zu messen.")
            return {"CANCELLED"}

        # STL carries no unit and every slicer reads millimetres, so the
        # numbers in the file have to BE millimetres. 1 Blender unit is 1 metre
        # for a WMV import, hence height_mm/1000 Blender units for the figure;
        # the factor that turns that into the requested millimetre height is
        # target / (height_mm/1000). Never 91.44, never 10 -- both are wrong,
        # and both were in earlier drafts of the plan.
        global_scale = context.scene.wmv_print_height_mm / (height_mm / BU_TO_MM)

        # Optionally sink the figure into the plate: cut the lowest millimetre
        # or two off and cap the cut flat. A WoW standing pose can touch the
        # plate on a single toe -- measured 1.5 x 1.1 mm on a real character,
        # which PrusaSlicer refuses outright -- and the cut turns that toe into
        # a flat pad. Done on a throwaway copy; the scene keeps its geometry.
        sink_mm = context.scene.wmv_print_sink_mm
        scale = context.scene.wmv_print_height_mm / height_mm
        temp = None
        if sink_mm > 0.0 and len(meshes) == 1:
            source = meshes[0]
            temp = source.copy()
            temp.data = source.data.copy()
            context.collection.objects.link(temp)
            bm = bmesh.new()
            bm.from_mesh(temp.data)
            bm.transform(source.matrix_world)
            temp.matrix_world = Matrix.Identity(4)
            cut_z = min(v.co.z for v in bm.verts) + sink_mm / scale / BU_TO_MM
            geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
            bmesh.ops.bisect_plane(bm, geom=geom, plane_co=(0.0, 0.0, cut_z),
                                   plane_no=(0.0, 0.0, 1.0),
                                   clear_inner=True, clear_outer=False)
            rim = [e for e in bm.edges if len(e.link_faces) == 1]
            if rim:
                bmesh.ops.holes_fill(bm, edges=rim, sides=0)
            bm.to_mesh(temp.data)
            bm.free()
            temp.data.update()
            export_set = [temp]
            floor_bu = cut_z
        else:
            if sink_mm > 0.0:
                self.report({"WARNING"},
                            "Einsinken gilt nur fuer eine unzerteilte Figur -- "
                            "%d Objekte, es wird ohne Schnitt exportiert."
                            % len(meshes))
            export_set = meshes
            floor_bu = min(m["z_mm"][0] for m in measurements) / BU_TO_MM

        # Sit the export on the plate. A WMV import straddles the origin, so an
        # untouched export puts part of the body below z=0 -- which a slicer
        # either clips or silently drops to the bed, and either way the numbers
        # in the file stop matching the numbers in the report.
        for obj in export_set:
            obj.location.z -= floor_bu
        context.view_layer.update()

        try:
            with context.temp_override(selected_objects=export_set,
                                       selected_editable_objects=export_set,
                                       object=export_set[0],
                                       active_object=export_set[0]):
                # Explicitly, both ways: the temp copy must be the ONLY thing
                # selected, or the original body ships twice in one file.
                for obj in context.scene.objects:
                    obj.select_set(obj in export_set)
                bpy.ops.wm.stl_export(
                    filepath=self.filepath,
                    export_selected_objects=True,
                    apply_modifiers=True,
                    global_scale=global_scale,
                    ascii_format=False,
                )
        finally:
            if temp is not None:
                bpy.data.objects.remove(temp, do_unlink=True)
            else:
                for obj in export_set:
                    obj.location.z += floor_bu
            context.view_layer.update()

        # The acceptance test is the written file, not the return value: a unit
        # mistake produces a perfectly valid STL of the wrong size, and this is
        # the only place it can still be caught.
        measured = _stl_bounds(self.filepath)
        if measured is None:
            self.report({"WARNING"},
                        "Datei geschrieben, aber nicht als binaere STL "
                        "lesbar: %s" % self.filepath)
            return {"FINISHED"}

        dx, dy, dz = measured["size"]
        contact_x, contact_y, _ = measured["contact"]
        target = context.scene.wmv_print_height_mm
        # The cut removes exactly sink_mm of printed height -- expected, not an
        # error, so the acceptance compares against what was asked for.
        expected = target - (sink_mm if temp is not None else 0.0)
        if abs(dz - expected) > max(0.5, 0.01 * target):
            self.report({"ERROR"},
                        "Geschrieben, aber die Datei ist %.1f mm hoch statt "
                        "%.0f mm. Nicht drucken." % (dz, expected))
            return {"CANCELLED"}

        name = os.path.basename(self.filepath)
        # Measured on two real characters: one stands on both soles and slices
        # straight through, the other on a single toe -- and PrusaSlicer answers
        # that one with "there is an object with no extrusions in the first
        # layer" and writes nothing. The pose decides it, and no earlier step in
        # the chain can see it.
        if min(contact_x, contact_y) < CONTACT_MIN_MM:
            self.report({"WARNING"},
                        "%s: %.0f x %.0f x %.0f mm -- ABER die Figur beruehrt "
                        "die Platte nur auf %.1f x %.1f mm. Ohne Brim oder Raft "
                        "nimmt der Slicer sie nicht an. Oder \"Einsinken\" auf "
                        "1-2 mm setzen: das schneidet ebene Standflaechen."
                        % (name, dx, dy, dz, contact_x, contact_y))
            return {"FINISHED"}
        self.report({"INFO"},
                    "%s: %d Dreiecke, %.0f x %.0f x %.0f mm, Standflaeche "
                    "%.0f x %.0f mm -- in der Datei nachgemessen"
                    % (name, measured["triangles"], dx, dy, dz,
                       contact_x, contact_y))
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Cutting for the build volume.
#
# DRUCK-IDEEN.md wanted this cut at a BONE -- "a cut at neck height disappears
# into the collar, a planar slicer cut lands across the face" -- on the grounds
# that only this tool knows where the neck is.
#
# It does not. The FBX names bones and vertex groups by index: bone_2, bone_17,
# bone_186, 132 of them on a character, with no anatomy attached. Nothing in the
# add-on can tell a neck from a forearm, and guessing would put the seam across
# the face exactly as feared. Carrying the M2 key-bone lookup into the sidecar
# would fix that, and it is an exporter change, not an add-on one.
#
# So the cut is geometric instead: it lands where the cross-section is
# NARROWEST, searched around each ideal division. That is a weaker claim than
# anatomy but the same instinct -- a neck, a waist and an ankle are all local
# minima of cross-section, and the narrowest cut is also the smallest glue seam
# and the least visible scar. It needs no bone names to be right.
# ---------------------------------------------------------------------------

# How far either side of an even division the narrowest spot is looked for.
# Wider finds better seams but drifts the parts away from equal height, and a
# part taller than the build volume is a failure, not a compromise.
SEGMENT_SEARCH = 0.15


def _cross_section_area(verts, z, slab):
    """Footprint of everything within +/- slab of height z, as a bounding-box
    area. A proxy for "how much material the saw goes through" -- crude, but it
    ranks candidate heights correctly, which is all that is asked of it."""
    xs, ys = [], []
    for vert in verts:
        if abs(vert.co.z - z) <= slab:
            xs.append(vert.co.x)
            ys.append(vert.co.y)
    if not xs:
        return 0.0
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def _segment_heights(verts, lo, hi, parts, max_window=None):
    """Pick parts-1 cut heights: near the even divisions, nudged to the
    narrowest cross-section nearby.

    max_window caps how far a cut may wander, in the same units as lo/hi. The
    caller sets it from the build-volume headroom, because a seam that looks
    better but pushes a slab past the build height has traded the whole purpose
    of cutting for a nicer scar."""
    if parts < 2:
        return []
    span = hi - lo
    slab = span * 0.01
    heights = []
    for index in range(1, parts):
        ideal = lo + span * index / float(parts)
        window = span * SEGMENT_SEARCH / parts
        if max_window is not None:
            window = min(window, max(0.0, max_window))
        best, best_area = ideal, None
        # 21 samples: fine enough to find a neck, cheap enough to not matter.
        for step in range(21):
            candidate = ideal - window + 2.0 * window * step / 20.0
            if not lo < candidate < hi:
                continue
            area = _cross_section_area(verts, candidate, slab)
            if area > 0.0 and (best_area is None or area < best_area):
                best, best_area = candidate, area
        heights.append(best)
    return heights


class WMV_OT_print_segment(bpy.types.Operator):
    """Cut the body into build-volume-sized parts at its narrowest points"""

    bl_idname = "wmv.print_segment"
    bl_label = "Zerteilen (Bauraum)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        profile = _print_profile(scene)
        meshes = _print_meshes(context)
        if len(meshes) != 1:
            self.report({"ERROR"},
                        "Zerteilen erwartet genau einen Koerper -- erst "
                        "\"4 - Vereinigen\" laufen lassen (%d Objekte da)."
                        % len(meshes))
            return {"CANCELLED"}

        obj = meshes[0]
        _, height_mm = _print_geometry(context)
        if height_mm <= 0.0:
            self.report({"ERROR"}, "Nichts zu messen.")
            return {"CANCELLED"}
        printed_z = profile["height_mm"]
        build_z = profile["build_z_mm"]
        whole = int(printed_z / build_z)
        parts = max(1, whole + (1 if printed_z > whole * build_z else 0))
        if parts < 2:
            self.report({"INFO"},
                        "%.0f mm passen in %.0f mm Bauhoehe -- nichts zu "
                        "zerteilen." % (printed_z, build_z))
            return {"FINISHED"}

        probe = bmesh.new()
        probe.from_mesh(obj.data)
        zs = [v.co.z for v in probe.verts]
        if not zs:
            probe.free()
            self.report({"ERROR"}, "Der Koerper hat keine Geometrie.")
            return {"CANCELLED"}
        low, high = min(zs), max(zs)
        # How far a cut may wander: half the headroom between an even slab and
        # the build height, so no slab can overflow however good the seam looks.
        # Measured on the orc, whose proportions let a 15%% search push a
        # 133 mm slab past a 150 mm build height.
        scale = printed_z / height_mm
        headroom_mm = max(0.0, build_z - printed_z / parts)
        max_window = (headroom_mm / 2.0) / scale / BU_TO_MM
        heights = _segment_heights(probe.verts, low, high, parts, max_window)
        probe.free()

        # One slab per part, each carved out of its own copy. The obvious
        # alternative -- cut once, then separate by loose parts -- sorts by
        # CONNECTIVITY instead of by slab: a remeshed body carries dozens of
        # small floating islands, and that produced 107 "parts" on a real
        # character where three were asked for.
        bounds = [low - 1.0] + heights + [high + 1.0]
        pieces = []
        for index in range(parts):
            piece = obj.copy()
            piece.data = obj.data.copy()
            context.collection.objects.link(piece)
            piece.name = "WMV_Druck_Teil_%02d" % (index + 1)

            bm = bmesh.new()
            bm.from_mesh(piece.data)
            for z, clear_below in ((bounds[index], True), (bounds[index + 1], False)):
                geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
                bmesh.ops.bisect_plane(
                    bm, geom=geom, plane_co=(0.0, 0.0, z),
                    plane_no=(0.0, 0.0, 1.0),
                    clear_inner=clear_below, clear_outer=not clear_below)
                # The cut leaves an open rim. The body was watertight before, so
                # every boundary edge now present belongs to this cut and capping
                # them all is exactly right.
                rim = [e for e in bm.edges if len(e.link_faces) == 1]
                if rim:
                    bmesh.ops.holes_fill(bm, edges=rim, sides=0)
            # Measured here, from the bmesh, and NOT from piece.dimensions
            # afterwards: dimensions comes from the bounding box and does not
            # refresh until the depsgraph does, so it would report the height of
            # the uncut body for every part.
            piece_zs = [v.co.z for v in bm.verts]
            extent = (max(piece_zs) - min(piece_zs)) if piece_zs else 0.0
            bm.to_mesh(piece.data)
            bm.free()
            piece.data.update()
            pieces.append((piece, extent))

        bpy.data.objects.remove(obj, do_unlink=True)

        # A slab that came out empty means the cut heights collapsed onto each
        # other -- better to say so than to hand over an empty object.
        empty = [piece for piece, _ in pieces if not piece.data.polygons]
        for piece in empty:
            bpy.data.objects.remove(piece, do_unlink=True)
        pieces = [(piece, extent) for piece, extent in pieces
                  if piece not in empty]

        tallest = max([extent * BU_TO_MM * scale for _, extent in pieces] or [0.0])
        if empty:
            self.report({"WARNING"}, "%d Scheiben blieben leer und wurden "
                                     "verworfen." % len(empty))

        if tallest > build_z * 1.02:
            self.report({"WARNING"},
                        "%d Teile, das hoechste ist %.0f mm bei %.0f mm "
                        "Bauhoehe -- erneut zerteilen." % (len(pieces), tallest,
                                                           build_z))
            return {"FINISHED"}
        self.report({"INFO"},
                    "In %d Teile geschnitten, hoechstes %.0f mm (Bauhoehe "
                    "%.0f mm)" % (len(pieces), tallest, build_z))
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Colour: the biggest unused advantage.
#
# For a WoW character colour IS the point, and hand-painting is a bigger hurdle
# than printing. The textures are already here -- packed into the FBX, wired
# into every material by the importer -- so both halves of the idea are built
# out of parts that already exist:
#
#   * a paint chart for people who paint themselves: one averaged colour per
#     material, as hex and as CIELAB
#   * a full-colour OBJ+MTL+textures ZIP for people who order a print
#
# CIELAB and not RGB, because RGB distance says a grey and a brown are close
# when the eye says they are not -- and matching paint by eye against an RGB
# number is exactly where that goes wrong. No manufacturer chart ships with
# this: Vallejo and Citadel tables are not freely licensed. Hex and LAB out,
# the matching is the user's.
# ---------------------------------------------------------------------------

# Enough samples for a stable average, few enough to stay instant on a 512x512
# texture -- and the count barely matters, since an average converges fast.
COLOUR_SAMPLES = 4096


def _linear_to_srgb(value):
    """Blender hands out scene-linear floats; every colour picker, paint chart
    and hex code in the world is sRGB."""
    if value <= 0.0031308:
        return max(0.0, 12.92 * value)
    return min(1.0, 1.055 * (value ** (1.0 / 2.4)) - 0.055)


def _srgb_to_hex(rgb):
    return "#%02X%02X%02X" % tuple(
        int(round(min(1.0, max(0.0, c)) * 255.0)) for c in rgb)


def _linear_to_lab(rgb):
    """Linear sRGB to CIELAB under D65. Pure arithmetic, so it is checked
    against known values in the offline test rather than by eye."""
    red, green, blue = rgb
    x = 0.4124564 * red + 0.3575761 * green + 0.1804375 * blue
    y = 0.2126729 * red + 0.7151522 * green + 0.0721750 * blue
    z = 0.0193339 * red + 0.1191920 * green + 0.9503041 * blue
    # D65 white point
    x, y, z = x / 0.95047, y / 1.00000, z / 1.08883

    def f(t):
        if t > 0.008856451679035631:  # (6/29)^3
            return t ** (1.0 / 3.0)
        return t / (3.0 * 0.20689655172413793 ** 2) + 4.0 / 29.0

    fx, fy, fz = f(x), f(y), f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def _material_average_colour(material):
    """Alpha-weighted average of the material's texture, in linear RGB.

    Weighted by alpha because a WoW texture's transparent regions are not part
    of what you would paint -- an unweighted average of a hair sheet returns the
    colour of its empty corners. Returns None when there is nothing to sample."""
    node = _find_image_node(material)
    image = node.image if node is not None else None
    if image is None or not image.has_data:
        return None
    width, height = image.size
    count = width * height
    if count <= 0:
        return None
    try:
        pixels = image.pixels[:]
    except (RuntimeError, MemoryError):
        return None

    stride = max(1, count // COLOUR_SAMPLES)
    totals = [0.0, 0.0, 0.0]
    weight = 0.0
    for index in range(0, count, stride):
        base = index * 4
        alpha = pixels[base + 3]
        if alpha <= 0.0:
            continue
        for channel in range(3):
            totals[channel] += pixels[base + channel] * alpha
        weight += alpha
    if weight <= 0.0:
        return None
    return tuple(total / weight for total in totals)


def _print_used_images(objects):
    """Every image actually wired into the given objects' materials, once each."""
    images = {}
    for obj in objects:
        for slot in obj.material_slots:
            if slot.material is None:
                continue
            node = _find_image_node(slot.material)
            if node is not None and node.image is not None:
                images[node.image.name] = node.image
    return list(images.values())


def _print_image_filename(image):
    """A flat, safe PNG filename for an image datablock. Blender's dedup suffix
    (".001") would otherwise become a bogus extension in the MTL."""
    stem = os.path.splitext(os.path.basename(image.name))[0]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem) or "textur"
    return stem + ".png"


class WMV_OT_print_paint_chart(bpy.types.Operator):
    """One averaged colour per material, as hex and CIELAB, for hand painting"""

    bl_idname = "wmv.print_paint_chart"
    bl_label = "Bemalvorlage (Farbkarte)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        materials = {}
        for obj in _print_meshes(context, include_hidden=True):
            for slot in obj.material_slots:
                if slot.material is not None:
                    materials[slot.material.name] = slot.material
        if not materials:
            self.report({"ERROR"},
                        "Keine Materialien -- die Farbkarte gehoert VOR "
                        "\"4 - Vereinigen\", solange die Teile noch getrennt "
                        "sind.")
            return {"CANCELLED"}

        rows, skipped = [], []
        for name in sorted(materials):
            material = materials[name]
            linear = _material_average_colour(material)
            if linear is None:
                skipped.append(name)
                continue
            srgb = tuple(_linear_to_srgb(c) for c in linear)
            lab = _linear_to_lab(linear)
            rows.append((name, _srgb_to_hex(srgb), lab,
                         bool(material.get("wmv_effect_plane"))))
        if not rows:
            self.report({"ERROR"}, "Keine Textur war lesbar (%d Materialien)."
                        % len(materials))
            return {"CANCELLED"}

        lines = ["WMV-Bemalvorlage", "=" * 64, "",
                 "Eine gemittelte Farbe je Material, alphagewichtet. CIELAB "
                 "statt RGB,",
                 "weil dort der Abstand dem entspricht, was das Auge sieht.",
                 "Herstellerfarben stehen bewusst nicht dabei -- die Tabellen "
                 "von Vallejo",
                 "und Citadel sind nicht frei lizenziert. Abgleich also selbst, "
                 "ueber L*a*b*.",
                 "",
                 "  %-38s %-8s %18s" % ("Material", "Hex", "L*a*b*"),
                 "  " + "-" * 62]
        for name, hexcode, lab, effect in rows:
            lines.append("  %-38s %-8s %6.1f %6.1f %6.1f%s"
                         % (name[:38], hexcode, lab[0], lab[1], lab[2],
                            "  (Effektflaeche)" if effect else ""))
        if skipped:
            lines += ["", "Ohne lesbare Textur (%d): %s"
                      % (len(skipped), ", ".join(skipped[:6]))]

        text = bpy.data.texts.get("WMV-Bemalvorlage")
        if text is None:
            text = bpy.data.texts.new("WMV-Bemalvorlage")
        text.clear()
        text.write("\n".join(lines) + "\n")
        for line in lines:
            print(line)
        self.report({"INFO"}, "%d Farben ermittelt -- Farbkarte im Texteditor "
                              "unter \"WMV-Bemalvorlage\"" % len(rows))
        return {"FINISHED"}


class WMV_OT_print_color_export(bpy.types.Operator, ExportHelper):
    """Full-colour OBJ with materials and textures, zipped for a print service"""

    bl_idname = "wmv.print_color_export"
    bl_label = "Vollfarbe exportieren (ZIP)"
    bl_options = {"REGISTER"}

    filename_ext = ".zip"
    filter_glob: StringProperty(default="*.zip", options={"HIDDEN"})

    # What the print services state; not a law of physics, but exceeding it is
    # a rejected upload rather than a worse print, so it is reported loudly.
    MAX_TRIS = 1000000
    MAX_MB = 64.0

    def execute(self, context):
        meshes = _print_meshes(context)
        if not meshes:
            self.report({"ERROR"}, "Keine sichtbaren Mesh-Objekte.")
            return {"CANCELLED"}
        if not any(slot.material for obj in meshes for slot in obj.material_slots):
            self.report({"ERROR"},
                        "Kein Objekt hat noch ein Material -- der Vollfarbweg "
                        "gehoert VOR \"4 - Vereinigen\".")
            return {"CANCELLED"}

        _, height_mm = _print_geometry(context)
        if height_mm <= 0.0:
            self.report({"ERROR"}, "Nichts zu messen.")
            return {"CANCELLED"}
        # Same reasoning as the STL path: OBJ carries no unit either, and every
        # service reads millimetres.
        global_scale = _print_profile(context.scene)["height_mm"] / (height_mm / BU_TO_MM)

        tris = sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in meshes)
        staging = tempfile.mkdtemp(prefix="wmv_colour_")
        obj_path = os.path.join(
            staging, os.path.splitext(os.path.basename(self.filepath))[0] + ".obj")
        restore = []
        try:
            # The textures are PACKED inside the FBX and have no file on disk,
            # so path_mode="COPY" finds nothing to copy and writes an MTL full
            # of dead references -- a valid-looking archive with no colour in
            # it. Write them out first, point each image at what was written,
            # and let the MTL carry bare filenames.
            for image in _print_used_images(meshes):
                target = os.path.join(staging, _print_image_filename(image))
                previous = (image.filepath_raw, image.file_format)
                try:
                    image.file_format = "PNG"
                    image.filepath_raw = target
                    image.save()
                    restore.append((image, previous))
                except (RuntimeError, OSError):
                    image.filepath_raw, image.file_format = previous

            with context.temp_override(selected_objects=meshes,
                                       selected_editable_objects=meshes,
                                       object=meshes[0], active_object=meshes[0]):
                for obj in meshes:
                    obj.select_set(True)
                bpy.ops.wm.obj_export(
                    filepath=obj_path,
                    export_selected_objects=True,
                    export_materials=True,
                    export_triangulated_mesh=True,
                    export_uv=True,
                    export_normals=True,
                    # STRIP: every texture is already beside the OBJ in the
                    # staging directory and everything lands in one flat ZIP,
                    # so bare filenames are what the MTL should carry.
                    path_mode="STRIP",
                    global_scale=global_scale,
                    apply_modifiers=True,
                )

            written = sorted(os.listdir(staging))
            with zipfile.ZipFile(self.filepath, "w",
                                 compression=zipfile.ZIP_DEFLATED) as archive:
                for name in written:
                    archive.write(os.path.join(staging, name), name)
        except (OSError, RuntimeError) as error:
            self.report({"ERROR"}, "Export fehlgeschlagen: %s" % error)
            return {"CANCELLED"}
        finally:
            for image, (path, fmt) in restore:
                image.filepath_raw, image.file_format = path, fmt
            for name in os.listdir(staging):
                try:
                    os.remove(os.path.join(staging, name))
                except OSError:
                    pass
            try:
                os.rmdir(staging)
            except OSError:
                pass

        size_mb = os.path.getsize(self.filepath) / 1048576.0
        textures = sum(1 for name in written
                       if os.path.splitext(name)[1].lower()
                       in (".png", ".jpg", ".jpeg", ".tga", ".bmp"))
        over = []
        if tris > self.MAX_TRIS:
            over.append("%d Dreiecke > %d" % (tris, self.MAX_TRIS))
        if size_mb > self.MAX_MB:
            over.append("%.1f MB > %.0f MB" % (size_mb, self.MAX_MB))
        if over:
            self.report({"WARNING"},
                        "%s geschrieben (%d Texturen), aber ueber den ueblichen "
                        "Dienstleistergrenzen: %s -- \"5 - Dreiecke reduzieren\" "
                        "davor laufen lassen."
                        % (os.path.basename(self.filepath), textures,
                           ", ".join(over)))
            return {"FINISHED"}
        self.report({"INFO"},
                    "%s: %d Dreiecke, %d Texturen, %.1f MB -- innerhalb der "
                    "ueblichen Dienstleistergrenzen"
                    % (os.path.basename(self.filepath), tris, textures, size_mb))
        return {"FINISHED"}


class WMV_OT_print_run_all(bpy.types.Operator):
    """Run steps 1 to 5 in order, stopping at the first refusal"""

    bl_idname = "wmv.print_run_all"
    bl_label = "Schritte 1-5 ausfuehren"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        steps = (
            ("Effektflaechen entfernen", bpy.ops.wmv.print_strip),
            ("Pose und Massstab einfrieren", bpy.ops.wmv.print_freeze),
            ("Dicke geben", bpy.ops.wmv.print_thicken),
            ("Vereinigen", bpy.ops.wmv.print_unify),
            ("Dreiecke reduzieren", bpy.ops.wmv.print_decimate),
        )
        for number, (name, operator) in enumerate(steps, start=1):
            # Named on failure, because "it came out wrong" is only actionable
            # once it says WHICH step it came out wrong in. An operator that
            # reports ERROR makes bpy.ops RAISE rather than return CANCELLED,
            # so both exits have to be caught or the chain dies with a traceback
            # and says nothing.
            try:
                cancelled = "CANCELLED" in operator()
                detail = "Die Meldung dieses Schritts steht darueber."
            except RuntimeError as error:
                cancelled = True
                detail = str(error).replace("Error: ", "").strip()
            if cancelled:
                self.report({"ERROR"}, "Bei Schritt %d abgebrochen (%s). %s"
                            % (number, name, detail))
                return {"CANCELLED"}
        self.report({"INFO"},
                    "Schritte 1-5 durch. Jetzt \"6 - STL schreiben\".")
        return {"FINISHED"}


_CLASSES = (
    IMPORT_SCENE_OT_wmv_fbx,
    WMV_OT_import_last_export,
    WMV_OT_print_profile_save,
    WMV_OT_print_profile_load,
    WMV_OT_print_profile_reset,
    WMV_OT_print_check,
    WMV_OT_print_strip,
    WMV_OT_print_freeze,
    WMV_OT_print_thicken,
    WMV_OT_print_unify,
    WMV_OT_print_decimate,
    WMV_OT_print_segment,
    WMV_OT_print_export,
    WMV_OT_print_paint_chart,
    WMV_OT_print_color_export,
    WMV_OT_print_run_all,
    WMV_PT_sidebar,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_entry)
    # On the scene rather than on the operator: the panel shows them, and a
    # second run keeps what was typed for the first.
    bpy.types.Scene.wmv_print_height_mm = FloatProperty(
        name="Hoehe (mm)",
        description="Wie hoch die gedruckte Figur werden soll",
        default=200.0, min=20.0, max=2000.0, soft_max=400.0)
    bpy.types.Scene.wmv_print_remesh = BoolProperty(
        name="Vereinigen per Remesh",
        description="Verschmilzt die einzelnen Schalen zu einem Koerper. "
                    "Kostet an jeder scharfen Kante etwa eine Voxelbreite -- "
                    "abschalten, wenn der Slicer die Ueberlappungen selbst "
                    "vereinigen soll",
        default=True)
    bpy.types.Scene.wmv_print_sink_mm = FloatProperty(
        name="Einsinken (mm)",
        description="Schneidet die untersten Millimeter ab und erzeugt ebene "
                    "Standflaechen -- fuer Posen, die die Platte nur auf einer "
                    "Zehenspitze beruehren. 0 schneidet nichts",
        default=0.0, min=0.0, max=3.0)
    bpy.types.Scene.wmv_print_max_tris = IntProperty(
        name="Dreiecke max.",
        description="Obergrenze nach dem Remesh. 0 schaltet die Reduktion ab. "
                    "Ein 0,4-mm-Remesh einer 200-mm-Figur liefert rund 2,4 "
                    "Millionen Dreiecke und 114 MB -- Druckdienstleister nehmen "
                    "haeufig hoechstens eine Million",
        default=1000000, min=0, max=20000000)
    # The six profile fields. Not a process dropdown on purpose -- see the
    # comment above PROFILE_DEFAULTS.
    bpy.types.Scene.wmv_print_feature_mm = FloatProperty(
        name="Kleinstes Merkmal (mm)",
        description="Duesendurchmesser beim Schmelzschichten ODER Pixelgroesse "
                    "beim Harzdruck -- dieselbe Rolle. Treibt Wandstaerke und "
                    "Voxelgroesse",
        default=PROFILE_DEFAULTS["feature_mm"], min=0.01, max=2.0)
    bpy.types.Scene.wmv_print_wall_mm = FloatProperty(
        name="Mindestwandstaerke (mm)",
        description="Zielwert fuer Solidify. 0 rechnet ihn als 3 x kleinstes "
                    "Merkmal aus",
        default=PROFILE_DEFAULTS["wall_mm"], min=0.0, max=20.0)
    bpy.types.Scene.wmv_print_build_x_mm = FloatProperty(
        name="Bauraum X (mm)", default=PROFILE_DEFAULTS["build_x_mm"],
        min=1.0, max=5000.0)
    bpy.types.Scene.wmv_print_build_y_mm = FloatProperty(
        name="Bauraum Y (mm)", default=PROFILE_DEFAULTS["build_y_mm"],
        min=1.0, max=5000.0)
    bpy.types.Scene.wmv_print_build_z_mm = FloatProperty(
        name="Bauraum Z (mm)", default=PROFILE_DEFAULTS["build_z_mm"],
        min=1.0, max=5000.0)
    bpy.types.Scene.wmv_print_overhang_deg = FloatProperty(
        name="Max. Ueberhang (Grad)",
        description="Ab wann Stuetzen noetig werden. 90 heisst: Ueberhaenge "
                    "sind gleichgueltig -- so beschreibt man Pulververfahren, "
                    "ohne sie beim Namen zu nennen",
        default=PROFILE_DEFAULTS["overhang_deg"], min=0.0, max=90.0)
    bpy.types.Scene.wmv_print_tilt_deg = FloatProperty(
        name="Kippung (Grad)",
        description="Empfohlene Kippung auf der Platte. Wird nur angezeigt, "
                    "eingestellt wird sie im Slicer",
        default=PROFILE_DEFAULTS["tilt_deg"], min=0.0, max=89.0)
    bpy.types.Scene.wmv_print_escape_hole_mm = FloatProperty(
        name="Austrittsloch (mm)",
        description="Mindestdurchmesser fuer Harzablauf oder Pulveraustritt. "
                    "0 heisst keine",
        default=PROFILE_DEFAULTS["escape_hole_mm"], min=0.0, max=50.0)


def unregister():
    del bpy.types.Scene.wmv_print_max_tris
    del bpy.types.Scene.wmv_print_sink_mm
    del bpy.types.Scene.wmv_print_remesh
    for _property in PROFILE_PROPERTIES.values():
        delattr(bpy.types.Scene, _property)
    del bpy.types.Scene.wmv_print_height_mm
    bpy.types.TOPBAR_MT_file_import.remove(_menu_entry)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
