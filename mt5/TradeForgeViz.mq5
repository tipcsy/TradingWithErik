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

string g_file;                    // TFV_<Symbol>.csv
bool   g_ind_done = false;        // az indikátorok fel vannak-e már rakva
string g_ma_name  = "";           // a felrakott SMA rövidneve (fő ablak, leszedéshez)

//+------------------------------------------------------------------+
//| A SAJÁT al-ablak-indikátorok (TFWPR, TFBANDS) leszedése MINDEN    |
//| al-ablakból. CSÖKKENŐ ablak- és index-sorrend → a törlés miatti   |
//| index-eltolódás nem hagy ki egyet (ez okozta az időkeret-váltós   |
//| halmozódást).                                                     |
//+------------------------------------------------------------------+
void RemoveOurWPRs()
{
   int wtot = (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL);
   for(int w = wtot - 1; w >= 1; w--)
      for(int idx = ChartIndicatorsTotal(0, w) - 1; idx >= 0; idx--)
      {
         string nm = ChartIndicatorName(0, w, idx);
         if(StringFind(nm, "TFWPR") == 0 || StringFind(nm, "TFBANDS") == 0)
            ChartIndicatorDelete(0, w, nm);
      }
}

//+------------------------------------------------------------------+
int OnInit()
{
   g_file = FilePrefix + _Symbol + ".csv";
   g_ind_done = false;
   g_ma_name  = "";
   RemoveOurWPRs();   // előző futás maradék WPR-jei (halmozódás ellen)
   EventSetTimer(TimerSeconds);
   RefreshFromFile();
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   // Az AUTO-felrakott indikátorokat leszedjük (a TradeForgeViz-hez tartoznak).
   // A rajz-objektumok (TFV_) SZÁNDÉKOSAN maradnak.
   RemoveOurWPRs();
   if(g_ma_name != "")
      ChartIndicatorDelete(0, 0, g_ma_name);
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

   string inds[];
   int nind = 0;
   string seen[];        // a MOSTANI fájlban szereplő objektum-nevek (mark)
   int nseen = 0;
   bool cleared = false;
   while(!FileIsEnding(h))
   {
      string ln = FileReadString(h);
      if(StringLen(ln) == 0)
         continue;
      if(StringFind(ln, "CLEAR") == 0)   // V-off: a saját objektumok törlése
      {
         ObjectsDeleteAll(0, FilePrefix);
         cleared = true;
         continue;
      }
      if(StringFind(ln, "IND;") == 0)   // indikátor-leírás — külön kezeljük
      {
         ArrayResize(inds, nind + 1);
         inds[nind] = ln;
         nind++;
         continue;
      }
      string nm = ApplyLine(ln);
      if(nm != "")
      {
         ArrayResize(seen, nseen + 1);
         seen[nseen] = nm;
         nseen++;
      }
   }
   FileClose(h);

   // MARK-AND-SWEEP: a fájl a kívánt állapot TELJES pillanatképe → a SAJÁT (TFV_)
   // objektumokból leszedjük azokat, amik NEM voltak a mostani fájlban (árvák: pl.
   // már érvénytelen belépő-vonal, elmozdult ablakhoz tartozó régi jelölés). Az
   // upsert így NŐ/mozgat, a söprés pedig eltakarít — nincs halmozódás.
   // A CLEAR ág külön van (már mindent törölt), ott nem söprünk.
   if(!cleared)
      SweepOrphans(FilePrefix, seen, nseen);

   // Az indikátorokat CSAK EGYSZER rakjuk fel (amint először látjuk az IND sorokat).
   if(!g_ind_done && nind > 0)
   {
      SetupIndicators(inds, nind);
      g_ind_done = true;
   }
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Egy sor feldolgozása: TYPE;NAME;...                              |
//| Visszaad: a felrakott objektum neve (a söpréshez), vagy "" ha a  |
//| sor nem rajz-objektum (ismeretlen/hibás típus).                 |
//+------------------------------------------------------------------+
string ApplyLine(string ln)
{
   string f[];
   int n = StringSplit(ln, ';', f);
   if(n < 2)
      return "";

   string type = f[0];
   string name = f[1];

   // RECT (SMA-szalag + M15 doboz) → a TradeForgeBands al-ablak rajzolja, itt kihagyjuk.
   if(type == "VLINE" && n >= 5) { UpsertVLine(name, f); return name; }
   else if(type == "TREND" && n >= 8) { UpsertTrend(name, f); return name; }
   else if(type == "ARROW" && n >= 7) { UpsertArrow(name, f); return name; }
   else if(type == "TEXT"  && n >= 7) { UpsertText(name, f); return name; }
   else if(type == "LABEL" && n >= 8) { UpsertLabel(name, f); return name; }
   return "";
}

//+------------------------------------------------------------------+
//| Árva-takarítás: a `prefix`-szel kezdődő objektumok közül azokat, |
//| amik NINCSENEK a `seen` (mostani fájl) listában, leszedi. Minden |
//| al-ablakon átmegy (a VLINE a 0-s ablakhoz van horgonyozva); a    |
//| TradeForgeBands TFB_ objektumaihoz nem nyúl (más prefix).        |
//+------------------------------------------------------------------+
void SweepOrphans(string prefix, string &seen[], int nseen)
{
   for(int i = ObjectsTotal(0, -1, -1) - 1; i >= 0; i--)
   {
      string nm = ObjectName(0, i, -1, -1);
      if(StringFind(nm, prefix) != 0)     // nem a miénk
         continue;
      if(!InArray(nm, seen, nseen))
         ObjectDelete(0, nm);
   }
}

//+------------------------------------------------------------------+
bool InArray(string s, string &arr[], int cnt)
{
   for(int i = 0; i < cnt; i++)
      if(arr[i] == s)
         return true;
   return false;
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
   int      st = (ArraySize(f) >= 9) ? (int)StringToInteger(f[8]) : 0;   // vonalstílus (opcionális)

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
   ObjectSetInteger(0, name, OBJPROP_STYLE, st);
}

//+------------------------------------------------------------------+
//| ARROW;name;t1;p1;code;r,g,b;width  (valós kötés nyíl-jelölése)   |
//| code: Wingdings nyíl-kód (233 fel = BUY, 234 le = SELL). A nyíl a |
//| gyertyához horgonyozva: BUY alul (ANCHOR_TOP), SELL felül.       |
//+------------------------------------------------------------------+
void UpsertArrow(string name, string &f[])
{
   datetime t1   = (datetime)StringToInteger(f[2]);
   double   p1   = StringToDouble(f[3]);
   int      code = (int)StringToInteger(f[4]);
   color    c    = StringToColor(f[5]);
   int      w    = (int)StringToInteger(f[6]);

   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_ARROW, 0, t1, p1);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
   else
   {
      ObjectMove(0, name, 0, t1, p1);
   }
   ObjectSetInteger(0, name, OBJPROP_ARROWCODE, code);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, (code == 234) ? ANCHOR_BOTTOM : ANCHOR_TOP);
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
//| A stratégia által HASZNÁLT indikátorok felrakása a chartra       |
//| IND;<MA|WPR>;<TF>;<period>;[szint1;szint2;…]                     |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES TfFromStr(string s)
{
   if(s == "M1")  return PERIOD_M1;
   if(s == "M5")  return PERIOD_M5;
   if(s == "M15") return PERIOD_M15;
   if(s == "M30") return PERIOD_M30;
   if(s == "H1")  return PERIOD_H1;
   if(s == "H4")  return PERIOD_H4;
   return PERIOD_CURRENT;
}

void SetupIndicators(string &inds[], int cnt)
{
   // Szalag/doboz AL-ABLAK (TradeForgeBands) — a TradeForgeViz vezérli, hogy
   // EGY indikátor rakjon fel mindent. FELTÉTEL: a TradeForgeBands.ex5 megvan.
   int bh = iCustom(_Symbol, PERIOD_CURRENT, "TradeForgeBands");
   if(bh != INVALID_HANDLE)
      ChartIndicatorAdd(0, (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL), bh);

   for(int i = 0; i < cnt; i++)
   {
      string f[];
      int n = StringSplit(inds[i], ';', f);
      if(n < 4)
         continue;
      string          kind   = f[1];
      ENUM_TIMEFRAMES tf     = TfFromStr(f[2]);
      int             period = (int)StringToInteger(f[3]);

      // f[4] = vonalszín ("r,g,b" vagy "-" = alapértelmezett); WPR-nél f[5..] szintek.
      if(kind == "MA")
      {
         int hnd = iMA(_Symbol, tf, period, 0, MODE_SMA, PRICE_CLOSE);
         if(hnd == INVALID_HANDLE)
            continue;
         if(ChartIndicatorAdd(0, 0, hnd))   // 0 = fő (ár) ablak
            g_ma_name = ChartIndicatorName(0, 0, ChartIndicatorsTotal(0, 0) - 1);
      }
      else if(kind == "WPR")
      {
         // Saját WPR (TradeForgeWPR): állítható szín + szintek. A matek a
         // stratégiáé. FELTÉTEL: a TradeForgeWPR.ex5 le van fordítva.
         color  clr = (n > 4 && f[4] != "-") ? StringToColor(f[4]) : clrBlack;
         double l1  = (n > 5) ? StringToDouble(f[5]) : -20.0;
         double l2  = (n > 6) ? StringToDouble(f[6]) : -50.0;
         double l3  = (n > 7) ? StringToDouble(f[7]) : -80.0;
         int hnd = iCustom(_Symbol, tf, "TradeForgeWPR", period, clr, l1, l2, l3);
         if(hnd == INVALID_HANDLE)
            continue;
         int win = (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL);   // új al-ablak
         ChartIndicatorAdd(0, win, hnd);   // a leszedést a RemoveOurWPRs intézi (név szerint)
      }
   }
}
//+------------------------------------------------------------------+
