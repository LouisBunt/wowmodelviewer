/*----------------------------------------------------------------------*\
| SceneLighting                                                           |
|                                                                         |
| The scene's fixed-function lights, independent of any widget.           |
|                                                                         |
| These lived inside the wx LightControl panel, which the renderer read    |
| every frame via g_modelViewer->lightControl->lights[i]. The panel is     |
| never actually shown (see modelviewer.cpp, where its dock pane was       |
| removed) -- it only ever served as the owner of this data. Pulling the   |
| data and the GL application out means the renderer no longer depends on  |
| a GUI object existing.                                                   |
\*----------------------------------------------------------------------*/

#ifndef _SCENELIGHTING_H_
#define _SCENELIGHTING_H_

#include "glm/glm.hpp"
#include "glm/gtc/type_ptr.hpp"

#include <GL/glew.h>

// OpenGL only supports a maximum of 8 lights without using extensions.
const size_t MAX_LIGHTS = 4;

enum LightType
{
  LIGHT_POSITIONAL = 0,
  LIGHT_SPOT,
  LIGHT_DIRECTIONAL
};

struct Light {
  bool enabled;  // Is the light on/off?
  bool relative;  // Is the light relative to the model? yes/no
  unsigned short type;  // type: 0 = positional, 1 = spot, 2 = directional

  float arc;    // The arc angle of degrees for the light

  float constant_int; // The intensity of the light/colour, also affects the 'focus' of the light. 0.0 being constant (even) lighting
  float linear_int; // light linear quadradic
  float quadradic_int; // light intensity quadradic

  glm::vec4 pos;    // the position, positional (w > 0) or directional (w = 0)
  glm::vec4 target;  // the position the lighting is directed at.
  glm::vec4 diffuse;  // The colour
  glm::vec4 ambience;  // the colour of the ambience
  glm::vec4 specular;  // colour of specular lighting
};

namespace SceneLighting
{
  const glm::vec4 defaultAmbience(1.0f, 1.0f, 1.0f, 1.0f);
  const glm::vec4 defaultDiffuse(1.0f, 1.0f, 1.0f, 1.0f);
  const glm::vec4 defaultSpecular(1.0f, 1.0f, 1.0f, 1.0f);

  // Seed the array with the defaults and switch light 0 on, mirroring what
  // LightControl::Init() did (minus its call into the widget's own Update()).
  inline void reset(Light* lights)
  {
    if (!lights)
      return;

    for (size_t i = 0; i < MAX_LIGHTS; i++) {
      lights[i].ambience = defaultAmbience;
      lights[i].diffuse = defaultDiffuse;
      lights[i].specular = defaultSpecular;
      lights[i].pos = glm::vec4(0.0f, 0.2f, 1.0f, 1.0f);
      lights[i].target = glm::vec4(0.0f, -1.0f, 0.0f, 1.0f);
      lights[i].enabled = false;
      lights[i].relative = false;
      lights[i].type = LIGHT_DIRECTIONAL;
      lights[i].constant_int = 1.0f;
      lights[i].linear_int = 0.0f;
      lights[i].quadradic_int = 0.0f;
      lights[i].arc = 90.0f;
    }

    // Turn on the first light by default
    lights[0].enabled = true;
    glEnable(GL_LIGHT0);
    glLightfv(GL_LIGHT0, GL_DIFFUSE, glm::value_ptr(lights[0].diffuse));
    glLightfv(GL_LIGHT0, GL_AMBIENT, glm::value_ptr(lights[0].ambience));
    glLightfv(GL_LIGHT0, GL_SPECULAR, glm::value_ptr(lights[0].specular));
    glLightfv(GL_LIGHT0, GL_POSITION, glm::value_ptr(lights[0].pos));
  }

  // Push the array into the fixed-function pipeline. Carried over verbatim from
  // LightControl::UpdateGL(), including its quirk of writing the colour/position
  // of every light into the ACTIVE light's slot -- that is existing behaviour and
  // changing it here would be an unrelated fix.
  inline void apply(const Light* lights, int activeLight)
  {
    if (!lights)
      return;

    float tar[3] = {0.0f, -1.0f, 0.0f};
    for (size_t i = 0; i < MAX_LIGHTS; i++) {
      GLuint lightID = GL_LIGHT0 + (GLuint)i;

      if (lights[i].enabled)
        glEnable(lightID);
      else
        glDisable(lightID);

      glLightfv(GL_LIGHT0 + activeLight, GL_DIFFUSE, glm::value_ptr(lights[i].diffuse));
      glLightfv(GL_LIGHT0 + activeLight, GL_AMBIENT, glm::value_ptr(lights[i].ambience));
      glLightfv(GL_LIGHT0 + activeLight, GL_SPECULAR, glm::value_ptr(lights[i].specular));
      glLightfv(GL_LIGHT0 + activeLight, GL_POSITION, glm::value_ptr(lights[i].pos));

      glLightf(lightID, GL_CONSTANT_ATTENUATION, 1.0f);
      glLightf(lightID, GL_LINEAR_ATTENUATION, 0.0f);
      glLightf(lightID, GL_QUADRATIC_ATTENUATION, 0.0f);
      glLightf(lightID, GL_SPOT_CUTOFF, 180.0f);

      glLightfv(lightID, GL_SPOT_DIRECTION, tar);

      if (lights[i].type == LIGHT_POSITIONAL) {
        glLightf(lightID, GL_CONSTANT_ATTENUATION, lights[activeLight].constant_int);
        glLightf(lightID, GL_LINEAR_ATTENUATION, lights[activeLight].linear_int);
        glLightf(lightID, GL_QUADRATIC_ATTENUATION, lights[activeLight].quadradic_int);

      } else if (lights[activeLight].type == LIGHT_SPOT) {
        glLightf(lightID, GL_SPOT_CUTOFF, lights[i].arc);      // Lighting arc
        glLightfv(lightID, GL_SPOT_DIRECTION, glm::value_ptr(lights[i].target));  // Lighting target
      }
    }
  }
}

#endif
