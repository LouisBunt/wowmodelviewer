#ifndef DARKTHEME_H
#define DARKTHEME_H

class wxWindow;
class wxAuiManager;

// Central visual language for the wxWidgets portion of WMV.  Keeping this in
// one place lets new dialogs inherit the same palette without changing UI code.
namespace DarkTheme
{
	void ApplyToWindow(wxWindow* window);
	void ApplyDockArt(wxAuiManager& manager);
	// Switches over the parts wxWidgets cannot recolour itself: the native window
	// caption and the scrollbars inside trees and lists, which otherwise stay
	// light grey against the dark panels.  No-op outside MSW.
	void ApplyNativeDarkMode(wxWindow* window);
}

#endif
