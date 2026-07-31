/*
 * GameDatabase.h
 *
 *  Created on: 7 Aug. 2017
 *      Author: Jeromnimo
 */

#ifndef _GAMEDATABASE_H_
#define _GAMEDATABASE_H_

#include <vector>
#include "sqlite3.h"

class DBFile;
class GameFile;

class QDomElement;
#include <QString>

#ifdef _WIN32
#    ifdef BUILDING_CORE_DLL
#        define _GAMEDATABASE_API_ __declspec(dllexport)
#    else
#        define _GAMEDATABASE_API_ __declspec(dllimport)
#    endif
#else
#    define _GAMEDATABASE_API_
#endif

class _GAMEDATABASE_API_ sqlResult
{
public:
  sqlResult() : valid(false), nbcols(0) {}
  ~sqlResult() { /* TODO :free char** */ }
  bool empty() { return values.size() == 0; }
  bool valid;
  int nbcols;
  std::vector<std::vector<QString> > values;
};

namespace core
{
  // table structures as defined in xml file
  class _GAMEDATABASE_API_ FieldStructure
  {
  public:
    FieldStructure() :
      name(""),
      type(""),
      isKey(false),
      needIndex(false),
    arraySize(1),
    id(0),
    store(true)
    {}

    virtual ~FieldStructure() {}

    QString name;
    QString type;
    bool isKey;
    bool needIndex;
    unsigned int arraySize;
    int id;

    // store="no" declares a field that exists in the RECORD but gets no column in the
    // database. A sparse table's fields sit inline and back to back, so the only way to
    // locate field N is to walk everything in front of it -- which means every preceding
    // field must be declared with its real width, whether or not anyone wants its value.
    // Without this, reaching ItemSparse.OverallQualityID (record index 67) would cost
    // roughly a hundred extra columns across 172k rows.
    bool store;
  };

  class _GAMEDATABASE_API_ TableStructure
  {
  public:
    TableStructure() :
      name(""),
      file("")
    {}

    virtual ~TableStructure();

    QString name;
    QString file;
    std::vector<FieldStructure *> fields;

    bool create();
    bool fill();

    virtual DBFile * createDBFile();
  };


  class _GAMEDATABASE_API_ GameDatabase
  {
  public:
    GameDatabase();
    GameDatabase(GameDatabase &);

    bool initFromXML(const QString & file);

    sqlResult sqlQuery(const QString &query);

    void setFastMode() { m_fastMode = true; }

    virtual ~GameDatabase();

    void addTable(TableStructure *);

    virtual core::TableStructure * createTableStructure() = 0;
    virtual core::FieldStructure * createFieldStructure() = 0;

    virtual void readSpecificTableAttributes(QDomElement &, core::TableStructure *) = 0;
    virtual void readSpecificFieldAttributes(QDomElement &, core::FieldStructure *) = 0;

  protected:
    // Adjust table/field structures for the game build actually loaded, after
    // they have been read from the base XML (e.g. refresh DB2 field positions
    // from WoWDBDefs for a build newer than the shipped schema). Default no-op.
    virtual void refreshStructures(std::vector<TableStructure *> &) {}

    // Create secondary indexes on hot join/lookup columns, after all tables are populated.
    // Issued idempotently (CREATE INDEX IF NOT EXISTS) so an existing on-disk cache picks
    // them up on the next launch without a full rebuild. Default no-op.
    virtual void createIndices() {}

  private:
    static int treatQuery(void *NotUsed, int nbcols, char ** values, char ** cols);
    static void logQueryTime(void* aDb, const char* aQueryStr, sqlite3_uint64 aTimeInNs);

    bool createDatabaseFromXML(const QString & file);
    bool readStructureFromXML(const QString & file);

    sqlite3 *m_db;

    std::vector<TableStructure * > m_dbStruct;

    bool m_fastMode;
  };

}


#endif /* _WOWDATABASE_H_ */
