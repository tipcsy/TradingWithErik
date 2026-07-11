//+------------------------------------------------------------------+
//|                                            TradeForgeBands.mq5    |
//|  A TradeForge sáv-állapotát egy DEDIKÁLT AL-ABLAKBAN, fix skálán  |
//|  (0..1) rajzolja — így zoomtól/scrolltól függetlenül MINDIG       |
//|  látható, és a teljes betöltött szélességet kitölti.             |
//|                                                                  |
//|  NÉGY sáv, PER-GYERTYA színbufferrel (DRAW_COLOR_HISTOGRAM2):     |
//|    • FENT (kék)      — M15 jelzési ablak nyitva                   |
//|    • (zöld/piros)    — SMA-irány (BUY/SELL)                       |
//|    • PIAC-ÁLLAPOT    — generikus, kódonként (0..8) színezve       |
//|    • LENT (szürke)   — no-trade óra                              |
//|                                                                  |
//|  Adat: a Python `TFV_<Symbol>.csv` `STATE` sorai, tagelve:        |
//|  STATE;<stratégia>;<epoch>;<notrade>;<dir>;<window>;<market>. Az  |
//|  „buta": pontosan azt rajzolja, amit kap (a no-trade maszkolást a |
//|  küldő végzi). A bufferbe töltés a gyertya-idő → állapot leképzés  |
//|  alapján megy, így M1 és M15 charton is illeszkedik.             |
//+------------------------------------------------------------------+
#property indicator_separate_window
#property indicator_buffers 12
#property indicator_plots   4
#property indicator_minimum 0.0
#property indicator_maximum 1.0

input int    TimerSeconds = 1;      // fájl-újraolvasás (mp)
input string FilePrefix   = "TFV_"; // ugyanaz, mint a TradeForgeViz-nél
input string InpStrategy  = "";     // Melyik STRATÉGIA sávjait mutassa (üres = MIND)
input int    BarWidth     = 3;      // a per-gyertya oszlop vastagsága (px)
input int    SubWinHeightPx = 110;  // az al-ablak magassága px (0 = ne állítsd, kézi húzás)
// NÉGY sáv az al-ablakban (0..1), FENTRŐL LEFELÉ: kék M15-ablak, SMA-trend
// (zöld/piros), PIAC-ÁLLAPOT (generikus, kódonként színezve), szürke no-trade.
input double BoxTop       = 0.95;   // M15 ablak (kék)
input double BoxBot       = 0.75;
input double RibbonTop    = 0.70;   // SMA-trend (zöld/piros)
input double RibbonBot    = 0.52;
input double MarketTop    = 0.47;   // PIAC-ÁLLAPOT (a szürke és a trend KÖZÖTT)
input double MarketBot    = 0.29;
input double NoTradeTop   = 0.24;   // no-trade órák (szürke)
input double NoTradeBot   = 0.05;

// Színbufferek — plotonként (érték-alsó, érték-felső, színindex).
double NtTop[],  NtBot[],  NtCol[];   // plot 0: no-trade (szürke)
double TrTop[],  TrBot[],  TrCol[];   // plot 1: trend (zöld/piros)
double WnTop[],  WnBot[],  WnCol[];   // plot 2: M15-ablak (kék)
double MsTop[],  MsBot[],  MsCol[];   // plot 3: piac-állapot (kódonként színezve)

// A fájlból beolvasott per-gyertya állapot (idő szerint növekvő).
datetime g_st_time[];
int      g_st_notrade[];
int      g_st_dir[];
int      g_st_window[];
int      g_st_mstate[];               // generikus piac-állapot kód (0..8)
int      g_nstate = 0;
int      g_step   = 900;              // állapot-lépésköz mp-ben (a state-időkből)

// Az OnCalculate-ből mentett gyertya-idők (a bufferbe töltéshez OnTimer-kor is).
datetime g_time[];
int      g_rates = 0;

string g_file;
bool   g_height_set = false;          // az al-ablak magasságát egyszer állítjuk

//+------------------------------------------------------------------+
//| Az al-ablak magasságának EGYSZERI beállítása (utána a user szabadon
//| húzhatja). A saját al-ablak indexét ChartWindowFind adja; ha még nincs
//| kész (indul az indikátor), a következő timer-tick újrapróbálja.        |
//+------------------------------------------------------------------+
void ApplyWindowHeight()
{
   if(SubWinHeightPx <= 0 || g_height_set)
      return;
   int w = ChartWindowFind();
   if(w < 0)
      return;
   ChartSetInteger(0, CHART_HEIGHT_IN_PIXELS, w, SubWinHeightPx);
   g_height_set = true;
}

//+------------------------------------------------------------------+
int OnInit()
{
   // Plot 0 — no-trade (szürke)
   SetIndexBuffer(0, NtTop, INDICATOR_DATA);
   SetIndexBuffer(1, NtBot, INDICATOR_DATA);
   SetIndexBuffer(2, NtCol, INDICATOR_COLOR_INDEX);
   PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_COLOR_HISTOGRAM2);
   PlotIndexSetInteger(0, PLOT_LINE_WIDTH, BarWidth);
   PlotIndexSetInteger(0, PLOT_COLOR_INDEXES, 1);
   PlotIndexSetInteger(0, PLOT_LINE_COLOR, 0, C'128,128,128');
   PlotIndexSetString(0, PLOT_LABEL, "TF NoTrade");

   // Plot 1 — trend (zöld BUY / piros SELL)
   SetIndexBuffer(3, TrTop, INDICATOR_DATA);
   SetIndexBuffer(4, TrBot, INDICATOR_DATA);
   SetIndexBuffer(5, TrCol, INDICATOR_COLOR_INDEX);
   PlotIndexSetInteger(1, PLOT_DRAW_TYPE, DRAW_COLOR_HISTOGRAM2);
   PlotIndexSetInteger(1, PLOT_LINE_WIDTH, BarWidth);
   PlotIndexSetInteger(1, PLOT_COLOR_INDEXES, 2);
   PlotIndexSetInteger(1, PLOT_LINE_COLOR, 0, C'0,170,0');    // BUY
   PlotIndexSetInteger(1, PLOT_LINE_COLOR, 1, C'220,0,0');    // SELL
   PlotIndexSetString(1, PLOT_LABEL, "TF Trend");

   // Plot 2 — M15 jelzési ablak (kék)
   SetIndexBuffer(6, WnTop, INDICATOR_DATA);
   SetIndexBuffer(7, WnBot, INDICATOR_DATA);
   SetIndexBuffer(8, WnCol, INDICATOR_COLOR_INDEX);
   PlotIndexSetInteger(2, PLOT_DRAW_TYPE, DRAW_COLOR_HISTOGRAM2);
   PlotIndexSetInteger(2, PLOT_LINE_WIDTH, BarWidth);
   PlotIndexSetInteger(2, PLOT_COLOR_INDEXES, 1);
   PlotIndexSetInteger(2, PLOT_LINE_COLOR, 0, C'0,120,255');
   PlotIndexSetString(2, PLOT_LABEL, "TF Window");

   // Plot 3 — PIAC-ÁLLAPOT (generikus): a `market_state` kód (0..8) → szín.
   // A kódok a core.regime.CODE-hoz igazodnak; más piac-osztályozónál a Python
   // ugyanezt a mezőt tölti, csak a jelentés/szín-legenda változhat.
   SetIndexBuffer(9,  MsTop, INDICATOR_DATA);
   SetIndexBuffer(10, MsBot, INDICATOR_DATA);
   SetIndexBuffer(11, MsCol, INDICATOR_COLOR_INDEX);
   PlotIndexSetInteger(3, PLOT_DRAW_TYPE, DRAW_COLOR_HISTOGRAM2);
   PlotIndexSetInteger(3, PLOT_LINE_WIDTH, BarWidth);
   PlotIndexSetInteger(3, PLOT_COLOR_INDEXES, 9);
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 0, C'70,70,70');    // 0 besorolatlan (sötét)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 1, C'0,170,0');     // 1 Szép Bika (zöld)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 2, C'220,0,0');     // 2 Szép Medve (piros)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 3, C'150,200,0');   // 3 Ideges Bika (olíva)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 4, C'255,140,0');   // 4 Ideges Medve (narancs)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 5, C'0,120,255');   // 5 Oldalazás (kék)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 6, C'80,110,120');  // 6 Érdektelenség (pala)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 7, C'200,0,200');   // 7 Bizonytalanság (magenta)
   PlotIndexSetInteger(3, PLOT_LINE_COLOR, 8, C'220,200,0');   // 8 Átmenet (sárga)
   PlotIndexSetString(3, PLOT_LABEL, "TF Market");

   // Az üres (nem rajzolt) gyertyák jelölése.
   for(int p = 0; p < 4; p++)
      PlotIndexSetDouble(p, PLOT_EMPTY_VALUE, EMPTY_VALUE);

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
}

//+------------------------------------------------------------------+
//| A gyertya-idők mentése + a bufferek feltöltése az aktuális        |
//| állapottal (a fix rajzolás a bufferekből megy).                  |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   g_rates = rates_total;
   ArrayResize(g_time, rates_total);
   for(int i = 0; i < rates_total; i++)
      g_time[i] = time[i];
   FillBuffers();
   return(rates_total);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   ApplyWindowHeight();
   RefreshFromFile();
}

//+------------------------------------------------------------------+
//| A fájl felolvasása: a STATE sorok betöltése az állapot-tömbökbe,  |
//| majd a bufferek újratöltése. A CLEAR (V-off) kiüríti az állapotot.|
//+------------------------------------------------------------------+
void RefreshFromFile()
{
   int h = FileOpen(g_file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      return;

   datetime tms[];
   int nt[], dr[], wn[], ms[];
   int cnt = 0;
   while(!FileIsEnding(h))
   {
      string ln = FileReadString(h);
      if(StringLen(ln) == 0)
         continue;
      if(StringFind(ln, "CLEAR") == 0)   // V-off → az összes állapot törlése
      {
         cnt = 0;
         ArrayResize(tms, 0); ArrayResize(nt, 0); ArrayResize(dr, 0);
         ArrayResize(wn, 0);  ArrayResize(ms, 0);
         continue;
      }
      if(StringFind(ln, "STATE;") != 0)  // csak a STATE sorok érdekelnek minket
         continue;
      string f[];
      int n = StringSplit(ln, ';', f);
      if(n < 6)                          // STATE;<strat>;t;notrade;dir;window[;market]
         continue;
      // Több-stratégia szűrő: csak a MI stratégiánk STATE sorai (f[1] = stratégia).
      if(InpStrategy != "" && f[1] != InpStrategy)
         continue;
      ArrayResize(tms, cnt + 1); ArrayResize(nt, cnt + 1);
      ArrayResize(dr,  cnt + 1); ArrayResize(wn, cnt + 1); ArrayResize(ms, cnt + 1);
      tms[cnt] = (datetime)StringToInteger(f[2]);
      nt[cnt]  = (int)StringToInteger(f[3]);
      dr[cnt]  = (int)StringToInteger(f[4]);
      wn[cnt]  = (int)StringToInteger(f[5]);
      ms[cnt]  = (n >= 7) ? (int)StringToInteger(f[6]) : 0;   // piac-állapot kód
      cnt++;
   }
   FileClose(h);

   // Az új állapot átvétele (a Python idő szerint növekvő sorrendben írja).
   ArrayResize(g_st_time, cnt); ArrayResize(g_st_notrade, cnt);
   ArrayResize(g_st_dir,  cnt); ArrayResize(g_st_window,  cnt);
   ArrayResize(g_st_mstate, cnt);
   for(int i = 0; i < cnt; i++)
   {
      g_st_time[i]    = tms[i];
      g_st_notrade[i] = nt[i];
      g_st_dir[i]     = dr[i];
      g_st_window[i]  = wn[i];
      g_st_mstate[i]  = ms[i];
   }
   g_nstate = cnt;

   // Lépésköz a legkisebb pozitív szomszéd-különbségből (M15 = 900 mp; hézagoknál
   // a nagyobb rés kimarad → az ottani gyertyák üresek maradnak).
   g_step = 900;
   for(int i = 1; i < cnt; i++)
   {
      int d = (int)(g_st_time[i] - g_st_time[i - 1]);
      if(d > 0 && d < g_step)
         g_step = d;
   }

   FillBuffers();
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| Minden chart-gyertyához megkeresi a hozzá tartozó M15 állapotot,  |
//| és a három sávot (buffer) feltölti; ahol nincs állapot → üres.   |
//+------------------------------------------------------------------+
void FillBuffers()
{
   for(int i = 0; i < g_rates; i++)
   {
      int s = FindState(g_time[i]);
      if(s < 0)
      {
         NtTop[i] = EMPTY_VALUE; NtBot[i] = EMPTY_VALUE;
         TrTop[i] = EMPTY_VALUE; TrBot[i] = EMPTY_VALUE;
         WnTop[i] = EMPTY_VALUE; WnBot[i] = EMPTY_VALUE;
         MsTop[i] = EMPTY_VALUE; MsBot[i] = EMPTY_VALUE;
         continue;
      }
      // No-trade sáv (szürke)
      if(g_st_notrade[s] == 1) { NtTop[i] = NoTradeTop; NtBot[i] = NoTradeBot; NtCol[i] = 0; }
      else                     { NtTop[i] = EMPTY_VALUE; NtBot[i] = EMPTY_VALUE; }
      // Trend sáv (zöld BUY=0 / piros SELL=1)
      if(g_st_dir[s] != 0)     { TrTop[i] = RibbonTop; TrBot[i] = RibbonBot;
                                 TrCol[i] = (g_st_dir[s] > 0) ? 0 : 1; }
      else                     { TrTop[i] = EMPTY_VALUE; TrBot[i] = EMPTY_VALUE; }
      // M15-ablak sáv (kék)
      if(g_st_window[s] == 1)  { WnTop[i] = BoxTop; WnBot[i] = BoxBot; WnCol[i] = 0; }
      else                     { WnTop[i] = EMPTY_VALUE; WnBot[i] = EMPTY_VALUE; }
      // Piac-állapot sáv — MINDIG rajzolva (a szín = a kód, 0..8)
      MsTop[i] = MarketTop; MsBot[i] = MarketBot; MsCol[i] = g_st_mstate[s];
   }
}

//+------------------------------------------------------------------+
//| A `t` gyertya-időhöz tartozó állapot-index: a legnagyobb olyan    |
//| state-idő, ami <= t ÉS t még a [state, state+g_step) sávjában van.|
//| Bináris keresés (a state-idők növekvők). -1, ha nincs.           |
//+------------------------------------------------------------------+
int FindState(datetime t)
{
   if(g_nstate == 0)
      return -1;
   int lo = 0, hi = g_nstate - 1, res = -1;
   while(lo <= hi)
   {
      int mid = (lo + hi) / 2;
      if(g_st_time[mid] <= t) { res = mid; lo = mid + 1; }
      else                    { hi = mid - 1; }
   }
   if(res < 0)
      return -1;
   if(t < g_st_time[res] + g_step)
      return res;
   return -1;
}
//+------------------------------------------------------------------+
