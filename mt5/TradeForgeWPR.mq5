//+------------------------------------------------------------------+
//|                                              TradeForgeWPR.mq5    |
//|  Williams %R a stratégia matekjával (core.indicator_engine.wpr): |
//|    (HH - Close) / (HH - LL) * -100,  tartomány 0 (rng)-nél -50.  |
//|  Állítható VONALSZÍN és 3 SZINT (extrém/trigger) — ezért nem a    |
//|  beépített iWPR-t használjuk (annak színe/szintje nem állítható   |
//|  programból a ChartIndicatorAdd után).                           |
//|  A TradeForgeViz rakja fel iCustom-mal a stratégia paramétereivel.|
//+------------------------------------------------------------------+
#property indicator_separate_window
#property indicator_buffers 1
#property indicator_plots   1
#property indicator_minimum -100
#property indicator_maximum 0
#property indicator_color1  clrBlack
#property indicator_label1  "%R"

input int    InpPeriod = 14;         // WPR periódus
input color  InpColor  = clrBlack;   // vonalszín
input double InpLvl1   = -20;        // szint 1 (felső extrém)
input double InpLvl2   = -50;        // szint 2 (trigger)
input double InpLvl3   = -80;        // szint 3 (alsó extrém / M1)
input double InpLvl4   = 0;          // szint 4 (opcionális; 0/pozitív = nincs)

double WprBuf[];

//+------------------------------------------------------------------+
int OnInit()
{
   SetIndexBuffer(0, WprBuf, INDICATOR_DATA);
   IndicatorSetInteger(INDICATOR_DIGITS, 2);
   IndicatorSetString(INDICATOR_SHORTNAME, "TFWPR(" + (string)InpPeriod + ")");

   PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_LINE);
   PlotIndexSetInteger(0, PLOT_LINE_COLOR, InpColor);
   PlotIndexSetInteger(0, PLOT_LINE_WIDTH, 1);
   PlotIndexSetString(0, PLOT_LABEL, "%R(" + (string)InpPeriod + ")");

   // Jelentős szintek — az indikátor SAJÁT szint-vonalai (állítható). Csak a valós
   // (negatív) szintek számítanak; a 0/pozitív érték „nincs" (pl. M1-nél a 4.).
   // A SORREND: extrém, trigger(ek), extrém → a SZÉLSŐ kettő (extrém) szürke, a
   // BELSŐK (trigger) narancsak.
   double lv[4] = {InpLvl1, InpLvl2, InpLvl3, InpLvl4};
   int cnt = 0;
   for(int i = 0; i < 4; i++)
      if(lv[i] < 0.0) cnt++;
   IndicatorSetInteger(INDICATOR_LEVELS, cnt);
   int j = 0;
   for(int i = 0; i < 4; i++)
   {
      if(lv[i] >= 0.0) continue;
      IndicatorSetDouble(INDICATOR_LEVELVALUE, j, lv[i]);
      IndicatorSetInteger(INDICATOR_LEVELSTYLE, j, STYLE_DOT);
      IndicatorSetInteger(INDICATOR_LEVELCOLOR, j, (j == 0 || j == cnt - 1) ? clrGray : clrOrange);
      j++;
   }
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   if(rates_total < InpPeriod)
      return(0);

   int start = (prev_calculated > 0) ? prev_calculated - 1 : InpPeriod - 1;
   for(int i = start; i < rates_total; i++)
   {
      double hh = high[i];
      double ll = low[i];
      for(int k = 1; k < InpPeriod; k++)
      {
         if(high[i - k] > hh) hh = high[i - k];
         if(low[i - k]  < ll) ll = low[i - k];
      }
      double rng = hh - ll;
      WprBuf[i] = (rng == 0.0) ? -50.0 : (hh - close[i]) / rng * -100.0;
   }
   return(rates_total);
}
//+------------------------------------------------------------------+
