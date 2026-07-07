//+------------------------------------------------------------------+
//|                                              TradeForgeViz.mq5    |
//|  TradeForge chart-vizualizáció: felolvassa a Python által írt     |
//|  Common\Files\TFV_<Symbol>.csv fájlt és kirajzolja az objektumokat|
//|                                                                  |
//|  Elv: UPSERT (létrehoz VAGY módosít stabil név alapján) — SOHA    |
//|  nem töröl. Így egy meglévő objektum (pl. SMA-doboz) csak NŐ,     |
//|  amíg tart a feltétel; nincs villódzás és nincs duplikátum.      |
//|                                                                  |
//|  Telepítés: másold a fájlt az MQL5\Indicators mappába, fordítsd  |
//|  (F7), majd húzd a kívánt chartra. A fájlt a Python az MT5 közös  |
//|  (Common) mappájába írja, ezért FILE_COMMON-nal olvassuk.        |
//+------------------------------------------------------------------+
#property indicator_chart_window
#property indicator_plots 0

input int    TimerSeconds = 1;       // Fájl-újraolvasás gyakorisága (mp)
input string FilePrefix   = "TFV_";  // Objektum-név és fájl prefix

string g_file;   // TFV_<Symbol>.csv

//+------------------------------------------------------------------+
int OnInit()
{
   g_file = FilePrefix + _Symbol + ".csv";
   EventSetTimer(TimerSeconds);
   RefreshFromFile();
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   // SZÁNDÉKOSAN nem törlünk objektumot: a kirajzolt jelzések maradjanak meg.
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   return(rates_total);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   RefreshFromFile();
}

//+------------------------------------------------------------------+
//| Fájl felolvasása és minden sor alkalmazása (upsert)              |
//+------------------------------------------------------------------+
void RefreshFromFile()
{
   int h = FileOpen(g_file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      return;   // még nincs fájl — nem hiba

   while(!FileIsEnding(h))
   {
      string ln = FileReadString(h);
      if(StringLen(ln) > 0)
         ApplyLine(ln);
   }
   FileClose(h);
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Egy sor feldolgozása: TYPE;NAME;...                              |
//+------------------------------------------------------------------+
void ApplyLine(string ln)
{
   // Törlés-direktíva: a Python a V (viz) gomb KI-kapcsolásakor írja ki.
   if(StringFind(ln, "CLEAR") == 0)
   {
      ObjectsDeleteAll(0, FilePrefix);
      return;
   }

   string f[];
   int n = StringSplit(ln, ';', f);
   if(n < 2)
      return;

   string type = f[0];
   string name = f[1];

   if(type == "RECT"  && n >= 8) UpsertRect(name, f);
   else if(type == "VLINE" && n >= 5) UpsertVLine(name, f);
   else if(type == "TREND" && n >= 8) UpsertTrend(name, f);
   else if(type == "TEXT"  && n >= 7) UpsertText(name, f);
   else if(type == "LABEL" && n >= 8) UpsertLabel(name, f);
}

//+------------------------------------------------------------------+
//| RECT;name;t1;p1;t2;p2;r,g,b;fill                                 |
//+------------------------------------------------------------------+
void UpsertRect(string name, string &f[])
{
   datetime t1 = (datetime)StringToInteger(f[2]);
   double   p1 = StringToDouble(f[3]);
   datetime t2 = (datetime)StringToInteger(f[4]);
   double   p2 = StringToDouble(f[5]);
   color    c  = StringToColor(f[6]);
   bool     fill = (f[7] == "1");

   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, p1, t2, p2);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   else
   {
      // MÓDOSÍTÁS: a sarkok mozgatása → a doboz nő/zsugorodik (nem új objektum).
      ObjectMove(0, name, 0, t1, p1);
      ObjectMove(0, name, 1, t2, p2);
   }
   ObjectSetInteger(0, name, OBJPROP_COLOR, c);
   ObjectSetInteger(0, name, OBJPROP_FILL, fill);
}

//+------------------------------------------------------------------+
//| VLINE;name;t1;r,g,b;width                                        |
//+------------------------------------------------------------------+
void UpsertVLine(string name, string &f[])
{
   datetime t1 = (datetime)StringToInteger(f[2]);
   color    c  = StringToColor(f[3]);
   int      w  = (int)StringToInteger(f[4]);

   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_VLINE, 0, t1, 0);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   else
   {
      ObjectMove(0, name, 0, t1, 0);
   }
   ObjectSetInteger(0, name, OBJPROP_COLOR, c);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, w);
}

//+------------------------------------------------------------------+
//| TREND;name;t1;p1;t2;p2;r,g,b;width  (sugár nélkül)              |
//+------------------------------------------------------------------+
void UpsertTrend(string name, string &f[])
{
   datetime t1 = (datetime)StringToInteger(f[2]);
   double   p1 = StringToDouble(f[3]);
   datetime t2 = (datetime)StringToInteger(f[4]);
   double   p2 = StringToDouble(f[5]);
   color    c  = StringToColor(f[6]);
   int      w  = (int)StringToInteger(f[7]);

   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2);
      ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   else
   {
      ObjectMove(0, name, 0, t1, p1);
      ObjectMove(0, name, 1, t2, p2);
   }
   ObjectSetInteger(0, name, OBJPROP_COLOR, c);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, w);
}

//+------------------------------------------------------------------+
//| TEXT;name;t1;p1;r,g,b;fontsize;szöveg                            |
//+------------------------------------------------------------------+
void UpsertText(string name, string &f[])
{
   datetime t1 = (datetime)StringToInteger(f[2]);
   double   p1 = StringToDouble(f[3]);
   color    c  = StringToColor(f[4]);
   int      fs = (int)StringToInteger(f[5]);
   string   txt = f[6];

   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_TEXT, 0, t1, p1);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   else
   {
      ObjectMove(0, name, 0, t1, p1);
   }
   ObjectSetString(0, name, OBJPROP_TEXT, txt);
   ObjectSetInteger(0, name, OBJPROP_COLOR, c);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, fs);
}

//+------------------------------------------------------------------+
//| LABEL;name;corner;x;y;r,g,b;fontsize;szöveg  (chart-sarokhoz)    |
//+------------------------------------------------------------------+
void UpsertLabel(string name, string &f[])
{
   int    corner = (int)StringToInteger(f[2]);
   int    x      = (int)StringToInteger(f[3]);
   int    y      = (int)StringToInteger(f[4]);
   color  c      = StringToColor(f[5]);
   int    fs     = (int)StringToInteger(f[6]);
   string txt    = f[7];

   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, name, OBJPROP_CORNER, corner);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetString(0, name, OBJPROP_TEXT, txt);
   ObjectSetInteger(0, name, OBJPROP_COLOR, c);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, fs);
}
//+------------------------------------------------------------------+
