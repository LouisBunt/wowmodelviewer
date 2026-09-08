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
 * PrintMesh.cpp -- see PrintMesh.h for what lives here and why it is Qt-free.
 */

#include "PrintMesh.h"

#include <algorithm>
#include <cstring>
#include <istream>
#include <limits>
#include <ostream>
#include <unordered_map>

namespace PrintMesh
{

void appendTriangle(Triangles& out, const glm::mat4& m,
                    const glm::vec3& a, const glm::vec3& b, const glm::vec3& c)
{
  Triangle t;
  t.v[0] = glm::vec3(m * glm::vec4(a, 1.0f));
  t.v[1] = glm::vec3(m * glm::vec4(b, 1.0f));
  t.v[2] = glm::vec3(m * glm::vec4(c, 1.0f));
  if (glm::determinant(glm::mat3(m)) < 0.0f)
    std::swap(t.v[1], t.v[2]);
  out.push_back(t);
}

Bounds bounds(const Triangles& tris)
{
  Bounds b;
  b.valid = !tris.empty();
  b.min = glm::vec3(std::numeric_limits<float>::max());
  b.max = glm::vec3(-std::numeric_limits<float>::max());
  for (const Triangle& t : tris)
    for (int i = 0; i < 3; i++)
    {
      b.min = glm::min(b.min, t.v[i]);
      b.max = glm::max(b.max, t.v[i]);
    }
  if (!b.valid)
    b.min = b.max = glm::vec3(0.0f);
  return b;
}

float fitExtent(Triangles& tris, int axis, float sizeMm)
{
  const Bounds b = bounds(tris);
  if (!b.valid || axis < 0 || axis > 2 || sizeMm <= 0.0f)
    return 0.0f;
  const float extent = b.max[axis] - b.min[axis];
  if (extent <= 1e-9f)
    return 0.0f;

  const float s = sizeMm / extent;
  // The figure straddles the origin in model space; the slicer wants it standing on z = 0
  // and centred, or the numbers in the file stop matching the numbers in the report.
  const glm::vec3 anchor(0.5f * (b.min.x + b.max.x), 0.5f * (b.min.y + b.max.y), b.min.z);
  for (Triangle& t : tris)
    for (int i = 0; i < 3; i++)
      t.v[i] = (t.v[i] - anchor) * s;
  return s;
}

float fitToHeight(Triangles& tris, float heightMm)
{
  return fitExtent(tris, 2, heightMm);
}

int longestAxis(const Bounds& b)
{
  const glm::vec3 s = b.size();
  int axis = 2;                                   // ties go to Z: a figure stands
  if (s.x > s[axis]) axis = 0;
  if (s.y > s[axis]) axis = 1;
  return axis;
}

size_t dropDegenerate(Triangles& tris, float minArea)
{
  const size_t before = tris.size();
  tris.erase(std::remove_if(tris.begin(), tris.end(), [minArea](const Triangle& t) {
    const glm::vec3 n = glm::cross(t.v[1] - t.v[0], t.v[2] - t.v[0]);
    return 0.5f * glm::length(n) < minArea;
  }), tris.end());
  return before - tris.size();
}

glm::vec2 contactFootprint(const Triangles& tris, float slabMm)
{
  const Bounds b = bounds(tris);
  if (!b.valid)
    return glm::vec2(0.0f);
  const float top = b.min.z + slabMm;
  glm::vec2 lo(std::numeric_limits<float>::max()), hi(-std::numeric_limits<float>::max());
  for (const Triangle& t : tris)
    for (int i = 0; i < 3; i++)
      if (t.v[i].z <= top)
      {
        lo = glm::min(lo, glm::vec2(t.v[i]));
        hi = glm::max(hi, glm::vec2(t.v[i]));
      }
  return hi - lo;
}

float openEdgeRatio(const uint32_t* indices, size_t count)
{
  if (!indices || count < 3)
    return 0.0f;
  std::unordered_map<uint64_t, uint32_t> edges;
  edges.reserve(count);
  const size_t tris = count / 3;
  for (size_t t = 0; t < tris; t++)
    for (int e = 0; e < 3; e++)
    {
      const uint32_t a = indices[t * 3 + e];
      const uint32_t b = indices[t * 3 + (e + 1) % 3];
      if (a == b)
        continue;                                     // a collapsed edge is not an edge
      const uint64_t key = (uint64_t(std::min(a, b)) << 32) | uint64_t(std::max(a, b));
      edges[key]++;
    }
  if (edges.empty())
    return 0.0f;
  size_t open = 0;
  for (const auto& kv : edges)
    if (kv.second == 1)
      open++;
  return (float)open / (float)edges.size();
}

namespace
{
  template <typename T> void put(std::ostream& out, const T& v)
  {
    out.write(reinterpret_cast<const char*>(&v), sizeof(T));
  }
  template <typename T> bool get(std::istream& in, T& v)
  {
    in.read(reinterpret_cast<char*>(&v), sizeof(T));
    return in.gcount() == (std::streamsize)sizeof(T);
  }
  void putVec(std::ostream& out, const glm::vec3& v)
  {
    put(out, v.x); put(out, v.y); put(out, v.z);
  }
  bool getVec(std::istream& in, glm::vec3& v)
  {
    return get(in, v.x) && get(in, v.y) && get(in, v.z);
  }
}

bool writeBinarySTL(std::ostream& out, const Triangles& tris, const char* header)
{
  if (tris.size() > (size_t)std::numeric_limits<uint32_t>::max())
    return false;

  char head[80];
  std::memset(head, 0, sizeof(head));
  if (header)
    std::strncpy(head, header, sizeof(head));       // pads with zeros, cuts long text
  // A header starting with "solid" makes some readers try to parse ASCII. Keep it away.
  if (std::strncmp(head, "solid", 5) == 0)
    head[0] = 'S';
  out.write(head, sizeof(head));

  const uint32_t count = (uint32_t)tris.size();
  put(out, count);
  for (const Triangle& t : tris)
  {
    // Recomputed from the winding, never copied from the model: a facet normal that disagrees
    // with the vertex order is exactly what makes a slicer report inverted faces.
    glm::vec3 n = glm::cross(t.v[1] - t.v[0], t.v[2] - t.v[0]);
    const float len = glm::length(n);
    n = (len > 0.0f) ? n / len : glm::vec3(0.0f);
    putVec(out, n);
    putVec(out, t.v[0]);
    putVec(out, t.v[1]);
    putVec(out, t.v[2]);
    const uint16_t attr = 0;
    put(out, attr);
  }
  return out.good();
}

bool readBinarySTL(std::istream& in, STLInfo& info, float slabMm)
{
  char head[80];
  in.read(head, sizeof(head));
  if (in.gcount() != (std::streamsize)sizeof(head))
    return false;
  uint32_t count = 0;
  if (!get(in, count))
    return false;

  Triangles tris;
  tris.reserve(std::min<uint32_t>(count, 1u << 22));
  for (uint32_t i = 0; i < count; i++)
  {
    glm::vec3 n;
    Triangle t;
    uint16_t attr;
    if (!getVec(in, n) || !getVec(in, t.v[0]) || !getVec(in, t.v[1]) || !getVec(in, t.v[2]) ||
        !get(in, attr))
      return false;
    tris.push_back(t);
  }

  info.triangles = count;
  info.bounds = bounds(tris);
  info.contact = contactFootprint(tris, slabMm);
  return true;
}

} // namespace PrintMesh
