#include "DarkTheme.h"

// wx/platform.h first: __WXMSW__ is defined by wx, so the guard below only works
// once some wx header has been seen.
#include <wx/platform.h>

#ifdef __WXMSW__
#include <windows.h>
#include <dwmapi.h>
#include <uxtheme.h>
#pragma comment(lib, "dwmapi.lib")
#pragma comment(lib, "uxtheme.lib")
#endif

#include <wx/aui/framemanager.h>
#include <wx/aui/dockart.h>
#include <wx/button.h>
#include <wx/choice.h>
#include <wx/combobox.h>
#include <wx/listbox.h>
#include <wx/listctrl.h>
#include <wx/notebook.h>
#include <wx/scrolbar.h>
#include <wx/stattext.h>
#include <wx/textctrl.h>
#include <wx/treectrl.h>
#include <wx/window.h>

namespace
{
	// Palette from the "WoW Model Viewer Redesign" mock-up.  Hex values are the
	// design tokens; the wxColour triples are their RGB equivalents.
	const wxColour kWindow(11, 13, 16);        // #0b0d10 app background
	const wxColour kPanel(14, 17, 20);         // #0e1114 docked panels
	const wxColour kInput(20, 24, 30);         // #14181e text fields, lists
	const wxColour kButton(24, 29, 35);        // #181d23 buttons at rest
	const wxColour kButtonHover(34, 40, 47);   // #22282f button hover
	const wxColour kText(232, 234, 238);       // #e8eaee primary text
	const wxColour kMutedText(138, 147, 160);  // #8a93a0 secondary text
	const wxColour kAccent(200, 161, 90);      // #c8a15a gold accent
	const wxColour kAccentLight(217, 182, 120);// #d9b678 accent highlight
	const wxColour kOnAccent(23, 19, 10);      // #17130a text on gold
	const wxColour kBorder(35, 40, 47);        // #23282f hairline borders

	bool IsInput(const wxWindow* window)
	{
		return window->IsKindOf(CLASSINFO(wxTextCtrl)) ||
			window->IsKindOf(CLASSINFO(wxChoice)) ||
			window->IsKindOf(CLASSINFO(wxComboBox)) ||
			window->IsKindOf(CLASSINFO(wxListBox));
	}

	bool IsDataView(const wxWindow* window)
	{
		return window->IsKindOf(CLASSINFO(wxTreeCtrl)) ||
			window->IsKindOf(CLASSINFO(wxListCtrl));
	}

	void AddButtonStates(wxButton* button)
	{
		// Native controls retain their platform focus indicator.  These colour
		// changes make hover and keyboard focus visible even on older wx/MSW builds.
		button->Bind(wxEVT_ENTER_WINDOW, [button](wxMouseEvent& event)
		{
			if (button->IsEnabled())
				button->SetBackgroundColour(kButtonHover);
			button->Refresh();
			event.Skip();
		});
		button->Bind(wxEVT_LEAVE_WINDOW, [button](wxMouseEvent& event)
		{
			button->SetBackgroundColour(kButton);
			button->Refresh();
			event.Skip();
		});
		button->Bind(wxEVT_SET_FOCUS, [button](wxFocusEvent& event)
		{
			// The accent is a light gold, so the label has to flip to the dark
			// on-accent colour or it becomes unreadable while focused.
			button->SetBackgroundColour(kAccent);
			button->SetForegroundColour(kOnAccent);
			button->Refresh();
			event.Skip();
		});
		button->Bind(wxEVT_KILL_FOCUS, [button](wxFocusEvent& event)
		{
			button->SetBackgroundColour(kButton);
			button->SetForegroundColour(kText);
			button->Refresh();
			event.Skip();
		});
	}

	void ApplySingleWindow(wxWindow* window)
	{
		if (!window)
			return;

		window->SetForegroundColour(kText);

		if (IsInput(window))
		{
			window->SetBackgroundColour(kInput);
			return;
		}

		if (IsDataView(window))
		{
			window->SetBackgroundColour(kInput);
			return;
		}

		if (wxButton* button = wxDynamicCast(window, wxButton))
		{
			button->SetBackgroundColour(kButton);
			// Empty buttons are colour swatches in the lighting controls.  Preserve
			// their semantic colour instead of replacing it on hover/focus.
			if (!button->GetLabel().empty())
				AddButtonStates(button);
			return;
		}

		window->SetBackgroundColour(window->IsTopLevel() ? kWindow : kPanel);
	}
}

void DarkTheme::ApplyToWindow(wxWindow* window)
{
	ApplySingleWindow(window);
	if (!window)
		return;

	const wxWindowList& children = window->GetChildren();
	for (wxWindowList::compatibility_iterator node = children.GetFirst(); node; node = node->GetNext())
		ApplyToWindow(node->GetData());
}

void DarkTheme::ApplyNativeDarkMode(wxWindow* window)
{
#ifdef __WXMSW__
	if (!window)
		return;

	if (HWND hwnd = static_cast<HWND>(window->GetHandle()))
	{
		if (window->IsTopLevel())
		{
			// DWMWA_USE_IMMERSIVE_DARK_MODE turns the native caption dark.  The
			// attribute is 20 since Windows 10 20H1; earlier builds used 19, so
			// fall back rather than leaving a white title bar on those.
			const BOOL dark = TRUE;
			if (FAILED(::DwmSetWindowAttribute(hwnd, 20, &dark, sizeof(dark))))
				::DwmSetWindowAttribute(hwnd, 19, &dark, sizeof(dark));
		}

		if (IsDataView(window))
			::SetWindowTheme(hwnd, L"DarkMode_Explorer", nullptr);
	}

	const wxWindowList& children = window->GetChildren();
	for (wxWindowList::compatibility_iterator node = children.GetFirst(); node; node = node->GetNext())
		ApplyNativeDarkMode(node->GetData());
#else
	(void)window;
#endif
}

void DarkTheme::ApplyDockArt(wxAuiManager& manager)
{
	wxAuiDockArt* art = manager.GetArtProvider();
	if (!art)
		return;

	art->SetColour(wxAUI_DOCKART_BACKGROUND_COLOUR, kWindow);
	art->SetColour(wxAUI_DOCKART_SASH_COLOUR, kBorder);
	art->SetColour(wxAUI_DOCKART_ACTIVE_CAPTION_COLOUR, kAccent);
	art->SetColour(wxAUI_DOCKART_ACTIVE_CAPTION_GRADIENT_COLOUR, kAccentLight);
	art->SetColour(wxAUI_DOCKART_INACTIVE_CAPTION_COLOUR, kPanel);
	art->SetColour(wxAUI_DOCKART_INACTIVE_CAPTION_GRADIENT_COLOUR, kPanel);
	// Gold caption -> dark caption text, otherwise the panel titles wash out.
	art->SetColour(wxAUI_DOCKART_ACTIVE_CAPTION_TEXT_COLOUR, kOnAccent);
	art->SetColour(wxAUI_DOCKART_INACTIVE_CAPTION_TEXT_COLOUR, kMutedText);
	art->SetColour(wxAUI_DOCKART_BORDER_COLOUR, kBorder);
	art->SetColour(wxAUI_DOCKART_GRIPPER_COLOUR, kMutedText);

	// The mock-up separates panels with hairlines and flat fills, not with the
	// chunky bevelled sashes and gradient captions wxAUI defaults to.
	art->SetMetric(wxAUI_DOCKART_GRADIENT_TYPE, wxAUI_GRADIENT_NONE);
	art->SetMetric(wxAUI_DOCKART_PANE_BORDER_SIZE, 1);
	art->SetMetric(wxAUI_DOCKART_SASH_SIZE, 4);
	art->SetMetric(wxAUI_DOCKART_CAPTION_SIZE, 22);
}
