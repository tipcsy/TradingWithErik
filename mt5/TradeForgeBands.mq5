//+------------------------------------------------------------------+
//|                                            TradeForgeBands.mq5    |
//|  A TradeForge sáv-állapotát egy DEDIKÁLT AL-ABLAKBAN, fix skálán  |
//|  (0..1) rajzolja — így zoomtól/scrolltól függetlenül MINDIG       |
//|  látható, és a teljes betöltött szélességet kitölti.             |
//|                                                                  |
//|  3 VAGY 4 sáv, PER-GYERTYA színbufferrel (DRAW_COLOR_HISTOGRAM2), |
//|  PARAMETRIKUSAN elosztva — FENTRŐL LEFELÉ:                        |
//|    • (kék)           — M15 jelzési ablak nyitva                   |
//|    • (zöld/piros)    — SMA-irány (BUY/SELL)                       |
//|    • PIAC-ÁLLAPOT    — CSAK ha a piac-viz BE (kódonként 0..8)      |
//|    • (szürke)        — no-trade óra                              |
//|  Ha a piac-viz KI (a Python market=-1-et küld), a piac-sáv KIMARAD|
//|  és 3-sávos (alacsonyabb) elrendezésre vált.                     |
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
input int    BandHeightPx = 27;     // EGY sáv magassága px (az al-ablak = sávszám × ez)

// Az elrendezés PARAMETRIKUS: a sávok a [0.05 .. 0.95] tartományban egyenletesen
// oszlanak el, FENTRŐL LEFELÉ. Sávok száma a PIAC-SÁV állapotától függ:
//   • piac BE  → 4 sáv: kék M15-ablak, SMA-trend (zöld/piros), PIAC-ÁLLAPOT, szürke no-trade
//   • piac KI  → 3 sáv: kék M15-ablak, SMA-trend, szürke no-trade   (a piac-sáv KIMARAD)
// A piac BE/KI onnan derül ki, hogy a fájl STATE sorai adnak-e >=0 piac-kódot
// (a Python -1-et küld, ha a piac-viz ki van kapcsolva / nincs piac-stratégia).
#define BAND_LO       0.05
#define BAND_HI       0.95
#define BAND_FILLFRAC 0.82          // a sáv a slot-jának hány százalékát tölti ki

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
int      g_st_mstate[];               // piac-állapot kód (-1 = nincs sáv; 0..8 = kód)
int      g_nstate = 0;
int      g_step   = 900;              // állapot-lépésköz mp-ben (a state-időkből)
bool     g_market_on = false;         // van-e piac-sáv (bármely STATE sor kódja >= 0)
int      g_nbands    = 3;             // aktív sávok száma (3 vagy 4)

// Az OnCalculate-ből mentett gyertya-idők (a bufferbe töltéshez OnTimer-kor is).
datetime g_time[];
int      g_rates = 0;

string g_file;
int    g_applied_h = -1;               // a MÁR beállított al-ablak-magasság (px)

//+------------------------------------------------------------------+
//| A `k`. sáv (0 = legfelső) függőleges helye N sávos elrendezésben, |
//| a [BAND_LO..BAND_HI] tartományban egyenletesen elosztva.          |
//+------------------------------------------------------------------+
void BandPos(int k, int n, double &top, double &bot)
{
   double slot = (BAND_HI - BAND_LO) / n;      // egy sávnyi hely
   double fill = slot * BAND_FILLFRAC;         // ebből a színes rész
   double slot_top = BAND_HI - k * slot;       // a slot teteje
   top = slot_top - (slot - fill) / 2.0;       // a fill a slot közepén
   bot = top - fill;
}

//+------------------------------------------------------------------+
//| Az al-ablak magassága = aktív sávszám × BandHeightPx. Csak akkor  |
//| állítjuk, ha a CÉL változott (piac BE/KI vált 3↔4 sávot) — így a  |
//| két váltás között a user szabadon húzhatja. BandHeightPx<=0 → kézi.|
//+------------------------------------------------------------------+
void ApplyWindowHeight()
{
   if(BandHeightPx <= 0)
      return;
   int target = g_nbands * BandHeightPx;
   if(target == g_applied_h)
      return;
   int w = ChartWindowFind();
   if(w < 0)
      return;
   ChartSetInteger(0, CHART_HEIGHT_IN_PIXELS, w, target);
   g_applied_h = target;
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
      ms[cnt]  = (n >= 7) ? (int)StringToInteger(f[6]) : -1;  // -1 = nincs piac-sáv
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

   // Piac-sáv BE, ha BÁRMELY állapot kódja >= 0 (a Python -1-et küld, ha a
   // piac-viz ki van kapcsolva) → 4 sáv; különben 3 sáv (a piac-sáv kimarad).
   g_market_on = false;
   for(int i = 0; i < cnt; i++)
      if(g_st_mstate[i] >= 0) { g_market_on = true; break; }
   g_nbands = g_market_on ? 4 : 3;

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
   // A sávok függőleges helye az AKTÍV sávszámból (parametrikus). Fentről lefelé:
   //   0=kék M15-ablak, 1=SMA-trend, [2=piac-állapot ha BE], utolsó=szürke no-trade.
   double wTop, wBot, tTop, tBot, mTop, mBot, nTop, nBot;
   BandPos(0, g_nbands, wTop, wBot);                 // M15-ablak (kék)
   BandPos(1, g_nbands, tTop, tBot);                 // SMA-trend
   if(g_market_on)
   {
      BandPos(2, g_nbands, mTop, mBot);              // piac-állapot
      BandPos(3, g_nbands, nTop, nBot);              // no-trade (a piac ALATT)
   }
   else
   {
      mTop = 0.0; mBot = 0.0;                        // nincs piac-sáv
      BandPos(2, g_nbands, nTop, nBot);              // no-trade a 3. helyen
   }

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
      if(g_st_notrade[s] == 1) { NtTop[i] = nTop; NtBot[i] = nBot; NtCol[i] = 0; }
      else                     { NtTop[i] = EMPTY_VALUE; NtBot[i] = EMPTY_VALUE; }
      // Trend sáv (zöld BUY=0 / piros SELL=1)
      if(g_st_dir[s] != 0)     { TrTop[i] = tTop; TrBot[i] = tBot;
                                 TrCol[i] = (g_st_dir[s] > 0) ? 0 : 1; }
      else                     { TrTop[i] = EMPTY_VALUE; TrBot[i] = EMPTY_VALUE; }
      // M15-ablak sáv (kék)
      if(g_st_window[s] == 1)  { WnTop[i] = wTop; WnBot[i] = wBot; WnCol[i] = 0; }
      else                     { WnTop[i] = EMPTY_VALUE; WnBot[i] = EMPTY_VALUE; }
      // Piac-állapot sáv — CSAK ha a piac-viz BE van ÉS a kód érvényes (>=0).
      if(g_market_on && g_st_mstate[s] >= 0)
                               { MsTop[i] = mTop; MsBot[i] = mBot; MsCol[i] = g_st_mstate[s]; }
      else                     { MsTop[i] = EMPTY_VALUE; MsBot[i] = EMPTY_VALUE; }
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
