# ----------------------------------------------------------------------------
# WoW Model Viewer: Midnight -- Blender FBX importer add-on.
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
    "version": (1, 2, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > WoW Model Viewer FBX (.fbx); 3D View > Sidebar > WMV",
    "description": "Import WMV-exported FBX with viewport-identical materials",
    "category": "Import-Export",
}

import json
import os
import re

import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper

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


_CLASSES = (
    IMPORT_SCENE_OT_wmv_fbx,
    WMV_OT_import_last_export,
    WMV_PT_sidebar,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_entry)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_entry)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
