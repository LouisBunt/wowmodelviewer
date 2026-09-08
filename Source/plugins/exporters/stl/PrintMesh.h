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
 * PrintMesh.h
 *
 * The geometry half of the STL exporter, kept free of Qt, GL and WoWModel on purpose:
 * everything in here works on plain triangles and streams, so tests/stltest can prove the
 * scaling, the plate placement, the winding and the file format without game data, without
 * a compiled upstream and without a window.
 */

#ifndef _PRINTMESH_H_
#define _PRINTMESH_H_

#include <cstdint>
#include <iosfwd>
#include <vector>

#include "glm/glm.hpp"

namespace PrintMesh
{
  struct Triangle { glm::vec3 v[3]; };
  typedef std::vector<Triangle> Triangles;

  // Thresholds shared with the Blender add-on's print pipeline
  // (blender_addon/io_import_wmv_fbx/__init__.py: CONTACT_MIN_MM, OPEN_SHEET_RATIO), so both
  // tools warn about the same figure for the same reason.
  //
  // CONTACT_MIN_MM: measured on two real characters, one stands on both soles and slices,
  // the other on a single toe (1.5 x 1.1 mm) and PrusaSlicer answers "no extrusions in the
  // first layer" and writes nothing. Below this footprint the file needs a raft or brim.
  const float CONTACT_MIN_MM = 5.0f;
  // How much of the bottom counts as touching the plate when the footprint is measured.
  const float CONTACT_SLAB_MM = 0.5f;
  // Share of open edges above which a part is a sheet with no thickness (a cape, a tabard, a
  // skirt): the slicer prints nothing for a zero-thickness surface. Blender thickens these.
  const float OPEN_SHEET_RATIO = 0.3f;
  // Anything smaller is a sliver that only produces slicer warnings.
  const float MIN_TRIANGLE_AREA_MM2 = 1e-6f;
  // Real-world size of one model unit. WoW models are in yards; see FBXHeaders.h SCALE_FACTOR
  // (91.44 cm) for the same number in the FBX exporter.
  const float MM_PER_MODEL_UNIT = 914.4f;

  // Appends a, b, c transformed by m. When m mirrors (negative determinant) the vertex order
  // is swapped so the cross-product normal STL readers compute still points outward -- the
  // same reason WoWModel::draw switches to GL_CW for a mirrored model.
  void appendTriangle(Triangles& out, const glm::mat4& m,
                      const glm::vec3& a, const glm::vec3& b, const glm::vec3& c);

  struct Bounds
  {
    glm::vec3 min, max;
    bool valid;
    glm::vec3 size() const { return valid ? (max - min) : glm::vec3(0.0f); }
  };
  Bounds bounds(const Triangles&);

  // Scales uniformly so the extent along `axis` (0 = X, 1 = Y, 2 = Z) equals sizeMm, then rests
  // the lowest point on z = 0 and centres X/Y on the origin -- where every slicer expects a
  // part. Returns the factor applied (mm per input unit), 0 when there is nothing to scale.
  float fitExtent(Triangles&, int axis, float sizeMm);

  // fitExtent on Z: the height of a standing figure.
  float fitToHeight(Triangles&, float heightMm);

  // Which axis the bounds are longest along (0..2). A figure stands along Z; a sword lies
  // along X and a shield across X/Y, and for those the requested size means the long side.
  int longestAxis(const Bounds&);

  // Removes triangles whose area is below minArea. Returns how many went.
  size_t dropDegenerate(Triangles&, float minArea);

  // X/Y extent of every vertex within slabMm of the lowest point: the footprint on the plate.
  glm::vec2 contactFootprint(const Triangles&, float slabMm);

  // Share of edges that belong to exactly one triangle, over an indexed triangle list (three
  // indices per triangle, `count` indices in all). 0 for a closed body, 1 for a lone sheet.
  float openEdgeRatio(const uint32_t* indices, size_t count);

  // Binary STL: 80-byte header, triangle count, then per triangle a normal (recomputed from the
  // winding, never taken from the model), three vertices and a zero attribute word. header is
  // copied and padded; longer text is cut.
  bool writeBinarySTL(std::ostream&, const Triangles&, const char* header);

  // Reads a binary STL back: the acceptance test is the written file, not the return value of
  // the writer -- a unit mistake produces a perfectly valid STL of the wrong size, and this is
  // the only place it can still be caught.
  struct STLInfo
  {
    uint32_t triangles;
    Bounds bounds;
    glm::vec2 contact;
  };
  bool readBinarySTL(std::istream&, STLInfo&, float slabMm);
}

#endif /* _PRINTMESH_H_ */
