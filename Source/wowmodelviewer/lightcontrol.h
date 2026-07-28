#ifndef LIGHTCONTROL_H
#define LIGHTCONTROL_H

// wxWidgets
#include <wx/wxprec.h>
#ifndef WX_PRECOMP
    #include <wx/wx.h>
#endif


// OpenGL headers
#include "glm/glm.hpp"

// Vector types
#include "util.h"

// MAX_LIGHTS, Light and the GL application live in games/wow now, so the renderer
// can light a scene without a widget being around. This panel is the editor on top
// of that data, nothing more.
#include "SceneLighting.h"

class LightControl: public wxWindow
{
  DECLARE_CLASS(LightControl)
    DECLARE_EVENT_TABLE()

  //GUI objects
  //wxStaticText *lblCol
  wxStaticText *lblDiff, *lblAmb, *lblSpec;
  wxStaticText *lblPos, *lblIntensity, *lblTar, *lblAlpha;
  wxTextCtrl *txtPosX, *txtPosY, *txtPosZ;
  wxTextCtrl *txtTarX, *txtTarY, *txtTarZ;
  wxCheckBox *enabled, *relative;
  //wxButton *colour, *update
  wxButton *diffuse, *ambience, *specular, *reset;
  wxComboBox *lightSel;
  wxSlider *cintensity, *lintensity, *qintensity, *alpha;
  wxRadioButton *positional, *spot, *directional;
  
  int activeLight;

  glm::vec3 DoSetColour(const glm::vec3 &defColor);
  glm::vec4 DoSetColour(const glm::vec4 &defColor);
public:
  
  LightControl(wxWindow* parent, wxWindowID id = wxID_ANY);
  ~LightControl();

  Light *lights;

  void Init();
  void Update();
  void UpdateGL();

  void SetColour();
  void SetAmbience();
  void SetDiffuse();
  void SetPos(glm::vec4 p);
  void SetTarget(glm::vec4 t);
  void SetSpecular();

  Light GetCurrentLight();
  glm::vec4 GetCurrentPos();
  glm::vec4 GetCurrentAmbience();

  // Functions to GUI object events
  void OnButton(wxCommandEvent &event);
  void OnCombo(wxCommandEvent &event);
  void OnText(wxCommandEvent &event);
  void OnCheck(wxCommandEvent &event);
  void OnRadio(wxCommandEvent &event);
  void OnScroll(wxScrollEvent &event);

};

#endif

