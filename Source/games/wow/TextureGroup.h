/*----------------------------------------------------------------------*\
| TextureGroup                                                            |
|                                                                         |
| A skin (texture variation) of a model, as resolved from                 |
| CreatureDisplayInfo: up to three texture files plus the particle-colour  |
| override and the geoset set that belong to that display id.              |
|                                                                         |
| This lived in the wx front-end's animcontrol.h, which meant anything     |
| wanting to talk about skins had to include a GUI header. It is pure data |
| over GameFile/QString/glm with no toolkit dependency, so it belongs      |
| here next to the model code that produces it.                            |
\*----------------------------------------------------------------------*/

#ifndef _TEXTUREGROUP_H_
#define _TEXTUREGROUP_H_

#include <set>
#include <vector>

#include <QString>

#include "glm/glm.hpp"

#include "GameFile.h"

typedef int GeosetNum;

class TextureGroup
{
  public:
    static const size_t num = 3;
    size_t count;
    int base;
    GameFile * tex[num];
    // tex gp is derived from CreatureDisplayInfo, not just a random skin in the folder:
    bool definedTexture;
    // For particle colour replacements:
    int particleColInd; // ID for ParticleColor.dbc
    int PCRIndex;  // index into PCRList - list of particle color replacement values
    std::set<GeosetNum> creatureGeosetData;  // Defines which geosets are switched on for a particular display ID of a model

    TextureGroup() : count(0), base(0)
    {
      for (size_t i=0; i<num; i++)
      {
        tex[i] = 0;
      }
      particleColInd = 0;
      PCRIndex = -1;
      creatureGeosetData.clear();
      definedTexture = false;
    }

    // default copy constr
    TextureGroup(const TextureGroup &grp)
    {
      for (size_t i=0; i<num; i++)
      {
        tex[i] = grp.tex[i];
      }
      base = grp.base;
      count = grp.count;
      particleColInd = grp.particleColInd;
      PCRIndex = grp.PCRIndex;
      creatureGeosetData = grp.creatureGeosetData;
      definedTexture = grp.definedTexture;
    }

    bool operator<(const TextureGroup &grp) const
    {
      if (!definedTexture && grp.definedTexture)
        return false;
      if (definedTexture && !grp.definedTexture)
        return true;
      QString texname1 = tex[0]->fullname();
      QString texname2 = grp.tex[0]->fullname();
      texname1 = texname1.mid(texname1.lastIndexOf("/"));
      texname2 = texname2.mid(texname2.lastIndexOf("/"));
      if(texname1 != texname2)
        return texname1 < texname2;
      for (size_t i=0; i<num; i++)
      {
        if (tex[i]<grp.tex[i]) return true;
        if (tex[i]>grp.tex[i]) return false;
      }
      if (particleColInd < grp.particleColInd)
        return true;
      if (creatureGeosetData < grp.creatureGeosetData)
        return true;
      return false;
    }

    bool operator==(const TextureGroup &grp) const
    {
      for (size_t i=0; i<num; i++)
      {
        if (tex[i] != grp.tex[i])
          return false;
      }
      if (particleColInd != grp.particleColInd)
        return false;
      if (creatureGeosetData != grp.creatureGeosetData)
        return false;
      return true;
    }

    bool operator!=(const TextureGroup &grp) const
    {
      return !((*this) == grp);
    }

};

typedef std::set<TextureGroup> TextureSet;
typedef std::vector<glm::vec4> particleColorSet; // Holds 3 particle colours: Start, Mid and End (of particle life), for cases where
                                             // particle colours are overridden by values from ParticleColor.dbc,
typedef std::vector<particleColorSet> particleColorReplacements; // Holds 3 colour sets. The particle will get its replacement
                                                                 // colour set from 0, 1 or 2, depending on whether its
                                                                 // ParticleColorIndex is set to 11, 12 or 13

#endif
