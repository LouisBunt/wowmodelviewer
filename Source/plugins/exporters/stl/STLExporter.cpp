/*----------------------------------------------------------------------*\
| This file is part of WoW Model Viewer                                  |
|                                                                        |
| WoW Model Viewer is free software: you can redistribute it and/or      |
| modify it under the terms of the GNU General Public License as         |
| published by the Free Software Foundation, either version 3 of the     |
| License, or (at your option) any later version.                        |
|                                                                        |
| WoW Model Viewer is distributed in the hope that it will be useful,    |
| but WITHOUT ANY WARRANTY; without even the implied warranty of         |
| MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the          |
| GNU General Public License for more details.                           |
|                                                                        |
| You should have received a copy of the GNU General Public License      |
| along with WoW Model Viewer.                                           |
| If not, see <http://www.gnu.org/licenses/>.                            |
\*----------------------------------------------------------------------*/

/*
 * STLExporter.cpp -- see STLExporter.h for what this plugin does and deliberately leaves out.
 */

#define _STLEXPORTER_CPP_
#include "STLExporter.h"
#undef _STLEXPORTER_CPP_

// Includes / class Declarations
//--------------------------------------------------------------------
// STL
#include <cmath>
#include <cwchar>
#include <fstream>
#include <map>
#include <vector>

// Qt
#include <QFileInfo>
#include <QLocale>
#include <QString>

// Externals
#include "glm/glm.hpp"
#include "glm/gtc/matrix_transform.hpp"
#include "glm/gtc/quaternion.hpp"

// Other libraries
#include "Bone.h"
#include "ModelRenderPass.h"
#include "WoWModel.h"

#include "GlobalSettings.h"
#include "logger/Logger.h"

// Current library
#include "PrintMesh.h"

// Namespaces used
//--------------------------------------------------------------------

// Beginning of implementation
//--------------------------------------------------------------------
namespace
{
  // This plugin is compiled with /utf-8 (its CMakeLists), so the umlauts and symbols in the
  // wide literals below arrive intact; QString::fromWCharArray reads them as they are.
  QString W(const wchar_t * s) { return QString::fromWCharArray(s); }

  // The report is read by a German user; "1,83 m" and "84.213 Dreiecke" are what they expect.
  QString num(double v, int decimals = 0)
  {
    return QLocale(QLocale::German, QLocale::Germany).toString(v, 'f', decimals);
  }

  // The one number the user is asked for, and its sanity bounds. 200 mm is where a common FDM
  // printer still resolves the details and the figure still fits the bed (DRUCK-KONZEPT.md).
  const double DEFAULT_HEIGHT_MM = 200.0;
  const double MIN_HEIGHT_MM = 10.0;
  const double MAX_HEIGHT_MM = 2000.0;
  // Above this the figure does not fit the Z of widespread printers (MK3S 210 mm,
  // CORE One 270 mm) without being cut -- which is the slicer's job, not ours.
  const double COMMON_BUILD_HEIGHT_MM = 250.0;
  // Below this many triangles an open part is trim, not a sheet worth a warning.
  const size_t OPEN_SHEET_MIN_TRIANGLES = 50;

  // Light, not matter. Mirrors _is_effect_plane() in the Blender add-on -- an unlit pass that
  // blends or adds is a particle/glow billboard, frozen into a flat quad -- and goes one step
  // further for the printer: an ADDITIVE pass is dropped even when it is lit, because whatever
  // it contributes is brightness, and brightness has no volume. Alpha-tested cut-outs (hair
  // cards, cloth edges, blend 1) are geometry and stay, as flat cards; the concept lists
  // rebuilding hair from the alpha texture under "what we do not build".
  bool isEffectPlane(const ModelRenderPass * p)
  {
    const bool additive = (p->blendmode == 3 || p->blendmode == 4);
    const bool blended = (p->blendmode >= 2);   // alpha blend, additive, modulate, modulate2x
    return additive || (p->unlit && blended);
  }

  // What a warning calls a part: the character geoset group ("Cloak", "Skirt") where there is
  // one, otherwise model and geoset id.
  QString partLabel(WoWModel * model, const ModelGeosetHD * g)
  {
    if (model->modelType == MT_CHAR)
    {
      const QString group = WoWModel::getCGGroupName((CharGeosets)(g->id / 100));
      if (!group.isEmpty())
        return group;
    }
    return QString("%1 (Geoset %2)").arg(QFileInfo(model->name()).baseName()).arg(g->id);
  }

  // Replicates Bone::calcMatrix, which wow.dll does not export, the way the FBX exporter's
  // srcBoneWorld does: m = T(pivot) * T(animTrans) * R * S * T(-pivot), world = parent * m.
  // Memoised per call. Billboarding is left out; skeleton bones do not use it.
  glm::mat4 boneWorld(WoWModel * mdl, int b, ssize_t anim, size_t time,
                      std::vector<glm::mat4> & cache, std::vector<char> & done)
  {
    if (b < 0 || b >= (int)mdl->bones.size())
      return glm::mat4(1.0f);
    if (done[b])
      return cache[b];
    done[b] = 1;
    Bone & bone = mdl->bones[b];
    glm::mat4 mm(1.0f);
    if (bone.rot.uses(anim) || bone.scale.uses(anim) || bone.trans.uses(anim))
    {
      mm = glm::translate(mm, bone.pivot);
      if (bone.trans.uses(anim)) mm = glm::translate(mm, bone.trans.getValue(anim, time));
      if (bone.rot.uses(anim))   mm = mm * glm::mat4_cast(bone.rot.getValue(anim, time));
      if (bone.scale.uses(anim)) mm = glm::scale(mm, bone.scale.getValue(anim, time));
      mm = glm::translate(mm, bone.pivot * -1.0f);
    }
    cache[b] = (bone.parent > -1) ? boneWorld(mdl, bone.parent, anim, time, cache, done) * mm : mm;
    return cache[b];
  }

  // The bone matrices to skin with: the ones the viewport last rendered (Bone::mat), or --
  // when no frame has been drawn yet, which is the case for a headless --export that runs
  // before the window shows -- the same matrices computed here for the model's current clip
  // and time. WoWModel::calcBones is private and Bone::calcMatrix is not exported, hence the
  // replica. What it leaves out is what only a running animation manager sets (secondary
  // clip, mouth clip, closed fists), and a run that has drawn no frame has none of those.
  // Bone::calc is what tells the two cases apart: initV3 clears it, calcMatrix sets it.
  std::vector<glm::mat4> boneMatrices(WoWModel * model)
  {
    std::vector<glm::mat4> out(model->bones.size(), glm::mat4(1.0f));
    bool posed = false;
    for (const Bone & b : model->bones)
      if (b.calc) { posed = true; break; }
    if (posed)
    {
      for (size_t i = 0; i < model->bones.size(); i++)
        out[i] = model->bones[i].mat;
      return out;
    }
    std::vector<char> done(model->bones.size(), 0);
    for (size_t b = 0; b < model->bones.size(); b++)
      boneWorld(model, (int)b, (ssize_t)model->anim, model->animtime, out, done);
    return out;
  }

  // Open a std::fstream on a path that arrives as std::wstring. MSVC takes the wide path
  // directly; elsewhere the plugin API's wstring is narrowed like the OBJ exporter does.
  template <typename Stream> void openBinary(Stream & s, const std::wstring & path, std::ios::openmode mode)
  {
#ifdef _WIN32
    s.open(path.c_str(), mode | std::ios::binary);
#else
    s.open(QString::fromStdWString(path).toLocal8Bit().constData(), mode | std::ios::binary);
#endif
  }
}


// Constructors
//--------------------------------------------------------------------


// Destructor
//--------------------------------------------------------------------


// Public methods
//--------------------------------------------------------------------
std::wstring STLExporter::menuLabel() const
{
  // "STL", not "STL (3D-Druck)": ExportController turns this into the format label, and
  // --export <Format>,<Pfad> matches that label verbatim.
  return L"STL...";
}

std::wstring STLExporter::fileSaveTitle() const
{
  return L"STL für den 3D-Druck speichern";
}

std::wstring STLExporter::fileSaveFilter() const
{
  return L"STL files (*.stl)|*.stl";
}

bool STLExporter::exportModel(Model * m, std::wstring target)
{
  m_lastError.clear();
  m_lastReport.clear();

  WoWModel * model = dynamic_cast<WoWModel *>(m);
  if (!model)
  {
    m_lastError = L"Kein WoW-Modell geladen.";
    return false;
  }

  // The requested height. Anything unparseable or absurd is refused rather than silently
  // replaced: a 20 mm figure written where 200 was meant looks like success in the dialog.
  const std::wstring raw = parameter(L"print.height_mm", std::to_wstring(DEFAULT_HEIGHT_MM));
  wchar_t * end = nullptr;
  const double heightMm = std::wcstod(raw.c_str(), &end);
  if (end == raw.c_str() || !(heightMm >= MIN_HEIGHT_MM && heightMm <= MAX_HEIGHT_MM))
  {
    m_lastError = W(L"Druckhöhe „%1“ ist keine Zahl zwischen %2 und %3 mm.")
                    .arg(QString::fromStdWString(raw)).arg(MIN_HEIGHT_MM).arg(MAX_HEIGHT_MM)
                    .toStdWString();
    return false;
  }

  LOG_INFO << "Exporting" << model->modelname.c_str() << "as STL for printing, height"
           << heightMm << "mm, to" << QString::fromStdWString(target);

  PrintMesh::Triangles tris;
  QStringList sheets;
  Counters counters;

  // The viewport's mirror is part of what the user sees, so it is part of what gets printed
  // (PrintMesh::appendTriangle fixes the winding). The uniform scale_ is not: the figure is
  // rescaled to the requested height regardless.
  glm::mat4 bodyWorld(1.0f);
  if (model->mirrored_)
    bodyWorld = glm::scale(glm::mat4(1.0f), glm::vec3(1.0f, -1.0f, 1.0f));

  // The body's bone matrices are needed whether or not the body is shown: the attachments
  // below hang off them even when the body itself is hidden (item view).
  const std::vector<glm::mat4> bodyBones = boneMatrices(model);
  const bool bodyShown = model->showModel;
  collect(model, bodyWorld, bodyBones, tris, sheets, counters);

  // Attachments: the same walk as FBXExporter::createMeshes() and the OBJ exporter, the same
  // world transform WMV renders with (ModelAttachment::setup: bone.mat, then the attachment
  // offset), and the same rule -- what the viewport hides, the export leaves out.
  size_t items = 0;
  for (WoWModel::iterator it = model->begin(); it != model->end(); ++it)
  {
    std::map<POSITION_SLOTS, WoWModel *> itemModels = (*it)->models();
    for (auto & slot : itemModels)
    {
      WoWModel * item = slot.second;
      if (!item || !item->showModel)
        continue;

      glm::mat4 world = bodyWorld;
      const int l = model->attLookup[slot.first];
      if (l > -1 && l < (int)model->atts.size())
      {
        const int b = model->atts[l].bone;
        if (b >= 0 && b < (int)bodyBones.size())
          world = bodyWorld * bodyBones[b] * glm::translate(glm::mat4(1.0f), model->atts[l].pos);
      }

      const std::vector<glm::mat4> itemBones = boneMatrices(item);
      const size_t before = tris.size();
      collect(item, world, itemBones, tris, sheets, counters);
      if (tris.size() > before)
        items++;
    }
  }

  if (tris.empty())
  {
    m_lastError = W(L"Nichts zu exportieren: kein sichtbares Teil mit druckbarer Geometrie. "
                    L"Modell oder Teil einblenden.").toStdWString();
    return false;
  }

  // Scale: from the model's own units (yards) to the requested millimetres, measured on the
  // kept geometry -- a gnome and a tauren are a factor two apart, so nothing is hardcoded.
  //
  // Which extent the number applies to: a character stands, so it is the height. A part on
  // its own does not stand -- a sword lies along X, a shield spreads across X/Y -- and there
  // the number means the long side; fitting a sword's Z thickness to 100 mm made it 526 mm
  // long. Creatures are not MT_CHAR but stand too, and for them the longest extent IS Z.
  const bool figure = (model->modelType == MT_CHAR) && bodyShown;
  const int axis = figure ? 2 : PrintMesh::longestAxis(PrintMesh::bounds(tris));
  const double realSizeMm = PrintMesh::bounds(tris).size()[axis] * PrintMesh::MM_PER_MODEL_UNIT;
  if (PrintMesh::fitExtent(tris, axis, (float)heightMm) <= 0.0f)
  {
    m_lastError = W(L"Das Modell hat keine Ausdehnung -- nichts zu skalieren.").toStdWString();
    return false;
  }
  // Slivers only produce slicer warnings. Dropped after scaling so the threshold is in mm^2;
  // if the extreme point of the figure sat on a sliver, the size moved and is fitted again.
  const size_t degenerate = PrintMesh::dropDegenerate(tris, PrintMesh::MIN_TRIANGLE_AREA_MM2);
  if (degenerate > 0 && PrintMesh::fitExtent(tris, axis, (float)heightMm) <= 0.0f)
  {
    m_lastError = W(L"Nach dem Entfernen entarteter Dreiecke bleibt keine Geometrie.").toStdWString();
    return false;
  }

  // Write.
  {
    std::ofstream out;
    openBinary(out, target, std::ios::out | std::ios::trunc);
    if (!out)
    {
      m_lastError = W(L"Datei kann nicht geschrieben werden: %1")
                      .arg(QString::fromStdWString(target)).toStdWString();
      return false;
    }
    const QString header = QString("%1 %2 STL mm height=%3")
                             .arg(QString::fromStdWString(GLOBALSETTINGS.appName()))
                             .arg(QString::fromStdWString(GLOBALSETTINGS.appVersion()))
                             .arg(heightMm);
    if (!PrintMesh::writeBinarySTL(out, tris, header.toLatin1().constData()))
    {
      m_lastError = W(L"Fehler beim Schreiben von %1").arg(QString::fromStdWString(target)).toStdWString();
      return false;
    }
    out.close();
    if (!out)
    {
      m_lastError = W(L"Fehler beim Schreiben von %1").arg(QString::fromStdWString(target)).toStdWString();
      return false;
    }
  }

  // Read back. The acceptance is the file on disk, not the buffer that was handed to the
  // writer -- and the height in the file is the one number a unit mistake would corrupt.
  PrintMesh::STLInfo info;
  {
    std::ifstream in;
    openBinary(in, target, std::ios::in);
    if (!in || !PrintMesh::readBinarySTL(in, info, PrintMesh::CONTACT_SLAB_MM))
    {
      m_lastError = W(L"Geschrieben, aber nicht als binäre STL lesbar: %1")
                      .arg(QString::fromStdWString(target)).toStdWString();
      return false;
    }
  }
  const glm::vec3 size = info.bounds.size();
  if (info.triangles != tris.size() ||
      std::fabs(size[axis] - heightMm) > std::max(0.5, 0.01 * heightMm))
  {
    m_lastError = W(L"Geschrieben, aber die Datei misst %1 mm statt %2 mm (%3 von %4 Dreiecken). "
                    L"Nicht drucken.")
                    .arg(num(size[axis], 1)).arg(num(heightMm)).arg(num((double)info.triangles))
                    .arg(num((double)tris.size())).toStdWString();
    return false;
  }

  // The report: what was written, in the numbers the slicer will show, plus the warnings the
  // slicer would otherwise be the first to raise. The box is given as X × Y × Z, the axes
  // every slicer labels the same way; a WoW model faces +X and stands along Z.
  QStringList lines;
  QString what;
  if (model->modelType != MT_CHAR)
    what = W(L"Modell");
  else if (!bodyShown)
    what = W(L"Nur Teil (Körper ausgeblendet): %1 Anhänge").arg(items);
  else if (items > 0)
    what = W(L"Ganze Figur mit %1 Anhängen").arg(items);
  else
    what = W(L"Ganze Figur");
  lines << W(L"%1 · %2 Dreiecke · %3 %4 mm · Box %5 × %6 × %7 mm (X × Y × Z)")
             .arg(what).arg(num((double)info.triangles))
             .arg(axis == 2 ? W(L"Höhe") : W(L"längste Seite")).arg(num(heightMm))
             .arg(num(size.x)).arg(num(size.y)).arg(num(size.z));
  // The scale line only means something for a thing that stands: a figure's model height is
  // its in-game height. A lone weapon's model is drawn at whatever size the artist chose
  // and is rescaled per wearer, so "aus 2,86 m" for a sword would be a number without a
  // meaning.
  if (axis == 2 && realSizeMm > 0.0)
    lines << W(L"Maßstab etwa 1:%1 — aus %2 m werden %3 mm.")
               .arg(num(realSizeMm / heightMm, 1)).arg(num(realSizeMm / 1000.0, 2)).arg(num(heightMm));

  const float contact = std::min(info.contact.x, info.contact.y);
  if (contact < PrintMesh::CONTACT_MIN_MM)
    lines << W(L"⚠ Standfläche nur %1 × %2 mm — das Teil berührt die Platte kaum. "
               L"Im Slicer auf eine Fläche legen (PrusaSlicer: Taste F) oder Raft/Brim "
               L"einschalten, sonst wird es abgewiesen.")
               .arg(num(info.contact.x, 1)).arg(num(info.contact.y, 1));
  else
    lines << W(L"Standfläche %1 × %2 mm.").arg(num(info.contact.x)).arg(num(info.contact.y));

  if (heightMm > COMMON_BUILD_HEIGHT_MM)
    lines << W(L"⚠ %1 mm passt auf die meisten Drucker nicht — im Slicer zerteilen "
               L"(PrusaSlicer: Taste C).").arg(num(heightMm));

  if (!sheets.isEmpty())
    lines << W(L"⚠ Ohne Dicke: %1 — solche Flächen druckt der Slicer nicht. "
               L"Vor dem Druck in Blender verdicken (WMV-Addon, „Für 3D-Druck“).")
               .arg(sheets.join(", "));

  if (counters.effectPasses > 0)
    lines << W(L"%1 Effektflächen (Glühen, Partikel-Ebenen) weggelassen.")
               .arg((int)counters.effectPasses);

  lines << (figure
              ? W(L"Die Datei ist maßstabsgerecht und steht auf der Platte. Wasserdicht "
                  L"(Nähte an Hals, Handgelenk, Knöchel) macht sie erst der Blender-Schritt.")
              : W(L"Die Datei ist maßstabsgerecht und liegt auf der Platte. Offene Kanten und "
                  L"dünne Flächen schließt erst der Blender-Schritt."));

  for (const QString & line : lines)
    LOG_INFO << line;
  m_lastReport = lines.join("\n").toStdWString();
  return true;
}

// Protected methods
//--------------------------------------------------------------------

// Private methods
//--------------------------------------------------------------------
void STLExporter::collect(WoWModel * model, const glm::mat4 & world,
                          const std::vector<glm::mat4> & bones, PrintMesh::Triangles & tris,
                          QStringList & sheets, Counters & c) const
{
  if (!model || !model->showModel)
    return;

  // The pose. model->vertices is a mapped VBO pointer that is only valid inside animate();
  // the OBJ exporter reads it anyway, which is one of its three bugs. Skin here from the bind
  // pose with the bone matrices boneMatrices() delivered -- the same arithmetic as
  // WoWModel::animate(), including its choice to skip an out-of-range bone rather than
  // renormalise, so the file shows what the viewport shows.
  const bool skin = model->animated && !bones.empty();
  std::vector<glm::vec3> posed(model->origVertices.size());
  for (size_t i = 0; i < model->origVertices.size(); i++)
  {
    const ModelVertex & ov = model->origVertices[i];
    if (!skin)
    {
      posed[i] = ov.pos;
      continue;
    }
    glm::vec3 v(0.0f);
    float weight = 0.0f;
    for (size_t b = 0; b < 4; b++)
    {
      if (ov.weights[b] == 0)
        continue;
      const size_t bi = ov.bones[b];
      if (bi >= bones.size())
        continue;
      const float w = (float)ov.weights[b] / 255.0f;
      v += glm::vec3(bones[bi] * glm::vec4(ov.pos, 1.0f)) * w;
      weight += w;
    }
    posed[i] = (weight > 0.0f) ? v : ov.pos;
  }

  // Which geosets. Visible passes only -- init(true) is the FBX exporter's gate, "visible at
  // some instant of its opacity animation", not "visible this instant". Each geoset once: a
  // multi-layer material references the same geoset once per layer, and a surface written
  // twice is precisely the doubled shell a slicer flags as non-manifold. A geoset whose every
  // visible pass is an effect plane is left out; one with any solid pass stays.
  std::map<int, bool> keep;   // geoIndex -> has at least one solid (printable) pass
  for (size_t i = 0; i < model->passes.size(); i++)
  {
    ModelRenderPass * p = model->passes[i];
    if (!p->init(true))
      continue;
    if (isEffectPlane(p))
    {
      c.effectPasses++;
      keep.insert(std::make_pair(p->geoIndex, false));   // does not overwrite a true
      continue;
    }
    keep[p->geoIndex] = true;
  }

  for (const auto & kv : keep)
  {
    if (!kv.second || kv.first < 0 || kv.first >= (int)model->geosets.size())
      continue;
    const ModelGeosetHD * g = model->geosets[kv.first];
    if (g->icount < 3 || (size_t)g->istart + g->icount > model->indices.size())
    {
      LOG_ERROR << "Geoset" << g->id << "of" << model->modelname.c_str()
                << "indexes past the index buffer -- skipped";
      continue;
    }

    const uint32 * idx = &model->indices[g->istart];
    const size_t faces = g->icount / 3;
    size_t written = 0;
    for (size_t f = 0; f < faces; f++)
    {
      const uint32 a = idx[f * 3], b = idx[f * 3 + 1], cc = idx[f * 3 + 2];
      if (a >= posed.size() || b >= posed.size() || cc >= posed.size())
        continue;
      PrintMesh::appendTriangle(tris, world, posed[a], posed[b], posed[cc]);
      written++;
    }
    if (written == 0)
      continue;
    c.geosets++;

    // A part whose edges are mostly open is a sheet: a cape, a tabard, a skirt. Measured on
    // the indexed geometry, before it becomes triangle soup and the information is gone.
    const float open = PrintMesh::openEdgeRatio(idx, g->icount);
    if (open > PrintMesh::OPEN_SHEET_RATIO && faces >= OPEN_SHEET_MIN_TRIANGLES)
    {
      const QString label = partLabel(model, g);
      if (!sheets.contains(label))
        sheets << label;
    }
  }
}
