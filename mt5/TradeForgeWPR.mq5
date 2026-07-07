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
input double InpLvl1   = -20;        // szint 1 (sell extrém)
input double InpLvl2   = -50;        // szint 2 (trigger)
input double InpLvl3   = -80;        // szint 3 (buy extrém)

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

   // Jelentős szintek — az indikátor SAJÁT szint-vonalai (állítható).
   IndicatorSetInteger(INDICATOR_LEVELS, 3);
   double lv[3] = {InpLvl1, InpLvl2, InpLvl3};
   for(int i = 0; i < 3; i++)
   {
      IndicatorSetDouble(INDICATOR_LEVELVALUE, i, lv[i]);
      IndicatorSetInteger(INDICATOR_LEVELSTYLE, i, STYLE_DOT);
      IndicatorSetInteger(INDICATOR_LEVELCOLOR, i, (i == 1) ? clrOrange : clrGray);
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
