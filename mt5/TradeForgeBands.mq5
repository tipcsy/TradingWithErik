//+------------------------------------------------------------------+
//|                                            TradeForgeBands.mq5    |
//|  A TradeForge SMA-irány szalagját (zöld/piros) és az M15 jelzési  |
//|  ablak dobozait (kék) egy DEDIKÁLT AL-ABLAKBAN jeleníti meg, fix  |
//|  skálán — így zoomtól/scrolltól függetlenül MINDIG látható.       |
//|                                                                  |
//|  Ugyanazt a Common\Files\TFV_<Symbol>.csv fájlt olvassa, mint a   |
//|  TradeForgeViz, de CSAK a RECT sorokat használja (szalag+doboz),  |
//|  a bennük lévő árat figyelmen kívül hagyva (fix al-ablak-pozíció).|
//|  UPSERT (create-or-move) → a doboz nő, nincs villódzás.          |
//+------------------------------------------------------------------+
#property indicator_separate_window
#property indicator_buffers 1
#property indicator_plots   1
#property indicator_minimum 0.0
#property indicator_maximum 1.0

input int    TimerSeconds = 1;      // fájl-újraolvasás (mp)
input string FilePrefix   = "TFV_"; // ugyanaz, mint a TradeForgeViz-nél
input double RibbonTop    = 0.45;   // SMA-szalag felső széle (al-ablak 0..1)
input double RibbonBot    = 0.05;   // SMA-szalag alsó széle
input double BoxTop       = 0.95;   // M15 doboz felső széle (a szalag FÖLÖTT)
input double BoxBot       = 0.55;   // M15 doboz alsó széle

double DummyBuf[];   // csak az al-ablak létrehozásához (nem rajzol)
string g_file;
int    g_win;        // a saját al-ablak indexe
string g_bpref = "TFB_";   // a SAJÁT objektumok prefixe (nem ütközik a fő ablak TFV_-jével)

//+------------------------------------------------------------------+
int OnInit()
{
   SetIndexBuffer(0, DummyBuf, INDICATOR_DATA);
   PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_NONE);
   IndicatorSetString(INDICATOR_SHORTNAME, "TFBANDS");
   IndicatorSetInteger(INDICATOR_DIGITS, 2);
   IndicatorSetDouble(INDICATOR_MINIMUM, 0.0);
   IndicatorSetDouble(INDICATOR_MAXIMUM, 1.0);
   g_file = FilePrefix + _Symbol + ".csv";
   EventSetTimer(TimerSeconds);
   RefreshFromFile();
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   // A saját al-ablak objektumait leszedjük (a TradeForgeViz-hez tartoznak).
   if(g_win >= 0)
      ObjectsDeleteAll(0, g_bpref, g_win);
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   return(rates_total);   // a buffer üres — csak az al-ablakhoz kell
}

//+------------------------------------------------------------------+
void OnTimer()
{
   RefreshFromFile();
}

//+------------------------------------------------------------------+
void RefreshFromFile()
{
   g_win = ChartWindowFind();   // a saját al-ablak aktuális indexe
   if(g_win < 0)
      return;

   int h = FileOpen(g_file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      return;

   while(!FileIsEnding(h))
   {
      string ln = FileReadString(h);
      if(StringLen(ln) == 0)
         continue;
      if(StringFind(ln, "CLEAR") == 0)   // V-off: a saját objektumok törlése
      {
         ObjectsDeleteAll(0, g_bpref, g_win);
         continue;
      }
      if(StringFind(ln, "RECT;") == 0)
         ApplyRect(ln);
   }
   FileClose(h);
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| RECT;name;t1;p1;t2;p2;r,g,b;fill  → fix al-ablak-pozícióba        |
//|   TFV_m15win* → felső (kék) sáv ; TFV_smaband* → alsó (zöld/piros)|
//+------------------------------------------------------------------+
void ApplyRect(string ln)
{
   string f[];
   int n = StringSplit(ln, ';', f);
   if(n < 7)
      return;
   string   name = f[1];
   datetime t1   = (datetime)StringToInteger(f[2]);
   datetime t2   = (datetime)StringToInteger(f[4]);
   color    c    = StringToColor(f[6]);

   double top, bot;
   if(StringFind(name, FilePrefix + "m15win") == 0)
   {
      top = BoxTop;    bot = BoxBot;      // M15 doboz — felül
   }
   else
   {
      top = RibbonTop; bot = RibbonBot;   // SMA-szalag — alul
   }

   // SAJÁT név (TFB_…) — hogy ne ütközzön a fő ablak esetleges TFV_ objektumaival.
   string oname = g_bpref + StringSubstr(name, StringLen(FilePrefix));

   if(ObjectFind(0, oname) < 0)
   {
      ObjectCreate(0, oname, OBJ_RECTANGLE, g_win, t1, top, t2, bot);
      ObjectSetInteger(0, oname, OBJPROP_FILL, true);
      ObjectSetInteger(0, oname, OBJPROP_BACK, true);
      ObjectSetInteger(0, oname, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, oname, OBJPROP_HIDDEN, true);
   }
   else
   {
      ObjectMove(0, oname, 0, t1, top);
      ObjectMove(0, oname, 1, t2, bot);
   }
   ObjectSetInteger(0, oname, OBJPROP_COLOR, c);
}
//+------------------------------------------------------------------+
