#ifndef ANIMCONTROL_H
#define ANIMCONTROL_H

#include <wx/wxprec.h>
#ifndef WX_PRECOMP
    #include <wx/wx.h>
#endif

//#include "model.h"
//#include "wmo.h"
#include "modelcanvas.h"

extern float animSpeed;

// AnimationData.dbc
#define ANIM_STAND  0

// TextureGroup and the skin/particle-colour typedefs moved to games/wow, so that
// non-GUI code can talk about skins without including a wx header.
#include "TextureGroup.h"

class AnimControl: public wxWindow
{
  DECLARE_CLASS(AnimControl)
  DECLARE_EVENT_TABLE()

  wxComboBox *animCList, *animCList2, *animCList3, *wmoList, *loopList;
  wxButton *showBLPList;
  wxStaticText *wmoLabel,*speedLabel, *speedMouthLabel, *frameLabel;
  wxStaticText *BLPSkinsLabel, *BLPSkinLabel1, *BLPSkinLabel2, *BLPSkinLabel3;
  wxSlider *speedSlider, *speedMouthSlider, *frameSlider;
  wxButton *btnAdd;
  wxCheckBox *lockAnims, *nextAnims;
  wxTextCtrl *lockText;

  wxButton *btnPlay, *btnPause, *btnStop, *btnClear, *btnPrev, *btnNext;
  wxCheckBox *oldStyle;

  bool UpdateCreatureModel(WoWModel *m);
  bool UpdateItemModel(WoWModel *m);
  bool FillSkinSelector(TextureSet &skins);
  bool FillBLPSkinSelector(TextureSet &skins, bool item = false);
  void UpdateFrameSlider(int maxRange, int tickFreq);

public:
  AnimControl(wxWindow* parent, wxWindowID id);
  ~AnimControl();

  wxComboBox *skinList, *BLPSkinList1, *BLPSkinList2, *BLPSkinList3;

  void UpdateModel(WoWModel *m);
  void UpdateWMO(WMO *w, int group);

  void OnButton(wxCommandEvent &event);
  void OnCheck(wxCommandEvent &event);
  void OnAnim(wxCommandEvent &event);
  void OnSkin(wxCommandEvent &event);
  void OnBLPSkin(wxCommandEvent &event);
  void OnItemSet(wxCommandEvent &event);
  void OnSliderUpdate(wxCommandEvent &event);
  void OnLoop(wxCommandEvent &event); 
  glm::vec4 fromARGB(int color);
  void SetSkinByDisplayID(int cdi);
  int AddSkin(TextureGroup grp);
  void SetSkin(int num);
  void ActivateBLPSkinList();
  void SyncBLPSkinList();
  void SetSingleSkin(int num, int texnum);
  void SetAnimSpeed(float speed);
  void SetAnimFrame(size_t frame);
  QString GetModelFolder(WoWModel *m);

  bool defaultDoodads; 
  std::string oldname;
  QString modelFolder;
  bool modelFolderChanged, BLPListFilled;
  std::map<int, TextureGroup> CDIToTexGp;
  std::vector<particleColorReplacements> PCRList; 
  int selectedAnim;
  int selectedAnim2;
  int selectedAnim3;
  bool bOldStyle;
  bool bLockAnims;
  bool bNextAnims;
  TextureSet BLPskins;
};

#endif

