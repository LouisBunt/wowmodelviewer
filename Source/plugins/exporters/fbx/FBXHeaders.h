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
 * FBXHeaders.h
 *
 *  Created on: 14 may 2019
 *   Copyright: 2019 , WoW Model Viewer (http://wowmodelviewer.net)
 */

#ifndef _FBXHEADERS_H_
#define _FBXHEADERS_H_

// Includes / class Declarations
//--------------------------------------------------------------------
// STL

// Qt
#include <qmutex.h>

// Externals
#include "fbxsdk.h"
#include "glm/glm.hpp"

// Other libraries
#include "WoWModel.h"

// Path separator. Defined here rather than pulled in from the GUI's util.h, which
// would drag wxWidgets into this plugin for nothing but this one character.
#ifdef _WINDOWS
  #define SLASH '\\'
#else
  #define SLASH '/'
#endif

// Current library


// Beginning of implementation
//--------------------------------------------------------------------
// WoW model units are yards; the scene is declared in centimetres (see createFBXHeaders), so
// scale by 1 yard = 91.44 cm. This makes a ~2-yard humanoid export at a realistic ~1.8 m in
// every DCC instead of the previous arbitrary 50x (which produced ~1 m characters). Applied
// uniformly to vertices, bone pivots, skeleton sizes and animation translation keys.
// (Was 50.0f; bumping it changes the real-world size of exports vs. earlier WMV builds.)
#define SCALE_FACTOR 91.44f

// Namespaces used
//--------------------------------------------------------------------


// Functions
//--------------------------------------------------------------------

namespace FBXHeaders
{
  // The FBX node name of bone i: "bone_<i>", plus "_<Role>" for the ~35 key bones WoW
  // itself names (Head, Jaw, ArmL, ...). One function, used by createSkeleton AND the
  // sidecar writer -- the sidecar's fbxName field must match the file or every
  // consumer's lookup goes nowhere. Format chosen deliberately (short, index first):
  // the index keeps siblings sorted and traceable to the M2, the role makes the bones
  // an artist touches readable, and dropping the model-name prefix keeps Blender's
  // outliner legible; two characters in one scene get Blender's own .001 suffixes.
  QString boneNodeName(WoWModel* model, int boneIndex);

  bool createFBXHeaders(FbxString fileVersion, QString l_FileName, FbxManager* &l_Manager, FbxExporter* &l_Exporter, FbxScene* &l_Scene);

  // outOldToNew, when given, receives one entry per model vertex: the exported control-point
  // index, or -1 for a vertex no VISIBLE pass references. Only the kept ones are written, so a
  // model whose geometry is mostly hidden -- the item view is the extreme case, one helmet out
  // of a whole character -- no longer drags every other vertex along as loose points. Anything
  // else addressing control points by model vertex index (the skin clusters) must translate
  // through this map, or the weights land on the wrong points.
  FbxNode* createMesh(FbxManager* &l_manager, FbxScene* &l_scene, WoWModel* model, const glm::mat4 & matrix = glm::mat4(1.0f), const glm::vec3 & offset = glm::vec3(0.0f), bool addUV2 = false, std::vector<int>* outOldToNew = nullptr);
  void createSkeleton(WoWModel* l_model, FbxScene* &l_scene, FbxNode* &l_skeletonNode, std::map<int, FbxNode*> &l_boneNodes);
  void storeBindPose(FbxScene* &l_scene, std::vector<FbxCluster*> l_boneClusters, FbxNode* l_meshNode);
  void storeRestPose(FbxScene* &l_scene, std::map<int, FbxNode*>& l_boneNodes);
  void createAnimation(WoWModel *l_model, FbxScene *& l_scene, QString animName, ModelAnimation cur_anim, std::map<int, FbxNode*>& skeleton);
}

// static members definition
#ifdef _FBXHEADERS_CPP_

#endif

#endif /* _FBXHEADERS_H_ */
