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
input string InpStrategy  = "";      // Melyik STRATÉGIÁT mutassa (üres = MIND)

string g_file;                    // TFV_<Symbol>.csv
string g_objpref;                 // szűrő-prefix: TFV_ (mind) VAGY TFV_<InpStrategy>@
string g_ind_sig  = "";           // az utoljára felrakott IND-halmaz aláírása
string g_ma_names[];              // MINDEN általunk felrakott MA rövidneve (leszedéshez)

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
//| A SAJÁT (általunk felrakott) MA-k leszedése a FŐ ablakból, név    |
//| szerint. Több MA is lehet (tf_align: idősíkonként egy) — a        |
//| g_ma_names MINDET számon tartja; azonos rövidnév (pl. két SMA100  |
//| más idősíkon) esetén az ismételt törlés egyenként szedi le.       |
//+------------------------------------------------------------------+
void RemoveOurMAs()
{
   for(int i = ArraySize(g_ma_names) - 1; i >= 0; i--)
      if(g_ma_names[i] != "")
         ChartIndicatorDelete(0, 0, g_ma_names[i]);
   ArrayResize(g_ma_names, 0);
}

//+------------------------------------------------------------------+
int OnInit()
{
   g_file = FilePrefix + _Symbol + ".csv";
   // Szűrő-prefix: ha van InpStrategy, csak a TFV_<strat>@ nevű objektumok a mieink
   // (a Python minden objektumot a stratégia nevével jelöl). Üres → minden TFV_.
   g_objpref = (InpStrategy == "") ? FilePrefix : (FilePrefix + InpStrategy + "@");
   g_ind_sig = "";
   ArrayResize(g_ma_names, 0);
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
   RemoveOurMAs();
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
         ObjectsDeleteAll(0, g_objpref);
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
      SweepOrphans(g_objpref, seen, nseen);

   // Indikátorok: az IND-halmaz ALÁÍRÁSA alapján. Ha VÁLTOZOTT (pl. a TF-együttállás
   // idősíkjai/SMA-ja átállt a dashboardon), a SAJÁT indikátorainkat leszedjük és
   // frissen felrakjuk — így a config-váltás azonnal látszik (nem csak restart után).
   // Változatlan halmaznál nem nyúlunk hozzá (nincs villódzás, nincs duplikátum).
   if(nind > 0)
   {
      string sig = "";
      for(int i = 0; i < nind; i++)
         sig += inds[i] + "|";
      if(sig != g_ind_sig)
      {
         RemoveOurMAs();
         RemoveOurWPRs();
         SetupIndicators(inds, nind);
         g_ind_sig = sig;
      }
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

   // Több-stratégia szűrő: csak a MI stratégiánk (g_objpref prefixű) objektumait
   // rajzoljuk. A Python minden nevet TFV_<strat>@… alakúra jelöl; InpStrategy
   // üresnél g_objpref="TFV_" → minden stratégia látszik.
   if(StringFind(name, g_objpref) != 0)
      return "";

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
   // EGY indikátor rakjon fel mindent. Átadjuk a szűrő-stratégiát is (input-
   // sorrend: TimerSeconds, FilePrefix, InpStrategy), hogy a sávok UGYANARRA a
   // stratégiára szűrjenek, mint a Viz. FELTÉTEL: a TradeForgeBands.ex5 megvan.
   int bh = iCustom(_Symbol, PERIOD_CURRENT, "TradeForgeBands",
                    TimerSeconds, FilePrefix, InpStrategy);
   if(bh != INVALID_HANDLE)
      ChartIndicatorAdd(0, (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL), bh);

   for(int i = 0; i < cnt; i++)
   {
      string f[];
      int n = StringSplit(inds[i], ';', f);
      if(n < 5)                              // IND;<strat>;kind;tf;period;...
         continue;
      // Több-stratégia szűrő: csak a MI stratégiánk indikátorai (f[1] = stratégia).
      if(InpStrategy != "" && f[1] != InpStrategy)
         continue;
      string          kind   = f[2];
      ENUM_TIMEFRAMES tf     = TfFromStr(f[3]);
      int             period = (int)StringToInteger(f[4]);

      // f[5] = vonalszín ("r,g,b" vagy "-" = alapértelmezett); WPR-nél f[6..] szintek.
      if(kind == "MA")
      {
         int hnd = iMA(_Symbol, tf, period, 0, MODE_SMA, PRICE_CLOSE);
         if(hnd == INVALID_HANDLE)
            continue;
         if(ChartIndicatorAdd(0, 0, hnd))   // 0 = fő (ár) ablak
         {
            // MINDEN felrakott MA nevét eltesszük (több is lehet: tf_align idősíkonként)
            string mn = ChartIndicatorName(0, 0, ChartIndicatorsTotal(0, 0) - 1);
            int k = ArraySize(g_ma_names);
            ArrayResize(g_ma_names, k + 1);
            g_ma_names[k] = mn;
         }
      }
      else if(kind == "WPR")
      {
         // Saját WPR (TradeForgeWPR): állítható szín + szintek. A matek a
         // stratégiáé. FELTÉTEL: a TradeForgeWPR.ex5 le van fordítva.
         color  clr = (n > 5 && f[5] != "-") ? StringToColor(f[5]) : clrBlack;
         double l1  = (n > 6) ? StringToDouble(f[6]) : -20.0;
         double l2  = (n > 7) ? StringToDouble(f[7]) : -50.0;
         double l3  = (n > 8) ? StringToDouble(f[8]) : -80.0;
         double l4  = (n > 9) ? StringToDouble(f[9]) : 0.0;   // opcionális 4. szint (M15: 2 trigger)
         int hnd = iCustom(_Symbol, tf, "TradeForgeWPR", period, clr, l1, l2, l3, l4);
         if(hnd == INVALID_HANDLE)
            continue;
         int win = (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL);   // új al-ablak
         ChartIndicatorAdd(0, win, hnd);   // a leszedést a RemoveOurWPRs intézi (név szerint)
      }
   }
}
//+------------------------------------------------------------------+
