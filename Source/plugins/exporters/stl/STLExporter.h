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
 * STLExporter.h
 *
 * STL for the 3D printer. Writes what the viewport shows -- the posed figure, the visible
 * geosets, the visible attachments -- as one binary STL in millimetres, scaled to a
 * requested height, standing on z = 0, without the glow and particle sheets that have no
 * matter to print. Then reads the file back and reports the measured size, the footprint on
 * the plate and the parts that have no thickness, so the slicer is not the first to complain.
 *
 * What it does NOT do, on purpose: thicken sheets, weld the open seams at wrist, ankle and
 * neck, or fuse the parts into one watertight body. Blender brings OpenVDB for that and the
 * WMV add-on's print pipeline drives it; a C++ copy would be a worse one (DRUCK-KONZEPT.md,
 * "Was wir nicht bauen").
 *
 * Settings arrive through ExporterPlugin::setParameter:
 *   "print.height_mm"  printed height in millimetres, default 200
 */

#ifndef _STLEXPORTER_H_
#define _STLEXPORTER_H_

// Includes / class Declarations
//--------------------------------------------------------------------
// STL
#include <string>
#include <vector>

// Qt
#include <QStringList>
#include <QtPlugin>

// Externals
class WoWModel;

// Other libraries
#include "glm/glm.hpp"

#define _EXPORTERPLUGIN_CPP_ // to define interface
#include "ExporterPlugin.h"
#undef _EXPORTERPLUGIN_CPP_

// Current library
#include "PrintMesh.h"

// Namespaces used
//--------------------------------------------------------------------


// Class Declaration
//--------------------------------------------------------------------
class STLExporter : public ExporterPlugin
{
    Q_INTERFACES(ExporterPlugin)
    Q_OBJECT
    Q_PLUGIN_METADATA(IID "wowmodelviewer.exporters.STLExporter" FILE "stlexporter.json")

  public :
    // Constants / Enums

    // Constructors
    STLExporter() {}

    // Destructors
    ~STLExporter() {}

    // Methods
    std::wstring menuLabel() const;

    std::wstring fileSaveTitle() const;
    std::wstring fileSaveFilter() const;

    bool exportModel(Model *, std::wstring file);

    // Members

  protected :
    // Constants / Enums

    // Constructors

    // Destructors

    // Methods

    // Members

  private :
    // Constants / Enums

    // Constructors

    // Destructors

    // Methods

    // Bookkeeping for the report.
    struct Counters
    {
      size_t effectPasses = 0;   // passes left out because they are light, not matter
      size_t geosets = 0;        // geosets written
    };

    // Appends every visible, printable geoset of one model, skinned with `bones` (one world
    // matrix per model bone) and transformed by world. Parts that are open sheets (no
    // thickness) are named in `sheets`.
    void collect(WoWModel * model, const glm::mat4 & world, const std::vector<glm::mat4> & bones,
                 PrintMesh::Triangles & tris, QStringList & sheets, Counters & c) const;

    // Members

    // friend class declarations

};

// static members definition
#ifdef _STLEXPORTER_CPP_

#endif

#endif /* _STLEXPORTER_H_ */
