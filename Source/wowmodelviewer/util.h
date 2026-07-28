#ifndef UTIL_H
#define UTIL_H

#ifdef _WINDOWS
#include <windows.h>
#endif

//#include <cstdlib>
#include <iostream>
#include <string>
#include <sstream>
#include <vector>

// Standard C++ headers

// Our other utility headers

#include <QString>

#include <wx/bitmap.h>
#include <wx/string.h>

using namespace std;

// Bridges for the remaining wx call sites. The process-wide path/locale globals below
// are QString (they are handed to core/wow, which are Qt-based), but the surrounding
// GUI is still wxWidgets. These two helpers keep the conversions in one place instead
// of scattering .c_str()/fromWCharArray gymnastics; they disappear along with the wx
// front-end.
inline wxString toWx(const QString& s) { return wxString(s.toStdWString()); }
inline QString fromWx(const wxString& s) { return QString::fromWCharArray(s.wc_str()); }

// These are process-wide and are handed straight to core/wow, which are Qt-based.
// Keeping them as QString removes the wxString <-> QString conversion dance at every
// boundary (the old code went through .c_str() and QString::fromWCharArray).
extern QString gamePath;
extern QString cfgPath;
extern QString bgImagePath;
extern QString armoryPath;
extern QString customDirectoryPath;
extern int customFilesConflictPolicy;
extern int displayItemAndNPCId;

extern bool useRandomLooks;

class UserSkins;
extern UserSkins& gUserSkins;

extern long langID;
extern QString langName;
extern long langOffset;
extern long interfaceID;
extern int ssCounter;
extern int imgFormat;
extern long versionID;

extern QString locales[];

// Slashes for Pathing.
// Plain char rather than wxT(): this macro is also used by the FBX exporter plugin,
// which should not have to pull in wxWidgets just for a path separator.
#ifdef _WINDOWS
  #define SLASH '\\'
#else
  #define SLASH '/'
#endif

float frand();


template <class T>
bool from_string(T& t, const string& s, ios_base& (*f)(ios_base&))
{
  istringstream iss(s);
  return !(iss >> f >> t).fail();
}

float round(float input, int limit);

wxString getGamePath(bool noSet = false);


#if defined _WINDOWS
wxBitmap* createBitmapFromResource(const wxString& t_name, long type = wxBITMAP_TYPE_PNG, int width = 0, int height = 0);
bool loadDataFromResource(char*& t_data, DWORD& t_dataSize, const wxString& t_name);
#endif

wxBitmap* getBitmapFromMemory(const char* t_data, const DWORD t_size, long type, int width, int height);

bool correctType(ssize_t type, ssize_t slot);

#endif

