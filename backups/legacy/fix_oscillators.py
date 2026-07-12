import re
with open("app/js/multi-chart.js", "r") as f:
    text = f.read()

new_oscillators = """
    if (showRSI && typeof TI.computeRSIArray === 'function') {
        const rsiArr = TI.computeRSIArray(closes, 14);
        cObj.rsiSeries.setData(rsiArr.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time));
    } else { cObj.rsiSeries.setData([]); }

    // Compute ATR Array
    if (showATR) {
        let atrData = [];
        let lastAtr = null;
        for (let i = 0; i < closes.length; i++) {
           if (i===0) { atrData.push(null); continue; }
           const tr = Math.max(highs[i]-lows[i], Math.abs(highs[i]-closes[i-1]), Math.abs(lows[i]-closes[i-1]));
           if (i < 14) {
             lastAtr = (lastAtr || 0) + tr;
             if (i === 13) { lastAtr = lastAtr/14; atrData.push(lastAtr); }
             else atrData.push(null);
           } else {
             lastAtr = ((lastAtr * 13) + tr) / 14;
             atrData.push(lastAtr);
           }
        }
        cObj.atrSeries.setData(atrData.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time));
    } else { cObj.atrSeries.setData([]); }

    // Compute ADX Array
    if (showADX) {
        let adxData = [];
        let plusDMArr = [], minusDMArr = [], trArr = [];
        for (let i=0; i<closes.length; i++) {
            if(i===0){ trArr.push(0); plusDMArr.push(0); minusDMArr.push(0); continue; }
            const up = highs[i]-highs[i-1], down = lows[i-1]-lows[i];
            plusDMArr.push((up > down && up > 0) ? up : 0);
            minusDMArr.push((down > up && down > 0) ? down : 0);
            trArr.push(Math.max(highs[i]-lows[i], Math.abs(highs[i]-closes[i-1]), Math.abs(lows[i]-closes[i-1])));
        }
        let smoothedTR = [], smoothedPlusDM = [], smoothedMinusDM = [], dx = [];
        let lastTR=0, lastPDM=0, lastMDM=0, lastADX=null;
        for (let i=0; i<closes.length; i++) {
            if(i===0){ smoothedTR.push(0); smoothedPlusDM.push(0); smoothedMinusDM.push(0); dx.push(0); adxData.push(null); continue; }
            if (i <= 14) {
               lastTR += trArr[i]; lastPDM += plusDMArr[i]; lastMDM += minusDMArr[i];
               smoothedTR.push(lastTR); smoothedPlusDM.push(lastPDM); smoothedMinusDM.push(lastMDM);
            } else {
               lastTR = lastTR - (lastTR/14) + trArr[i];
               lastPDM = lastPDM - (lastPDM/14) + plusDMArr[i];
               lastMDM = lastMDM - (lastMDM/14) + minusDMArr[i];
               smoothedTR.push(lastTR); smoothedPlusDM.push(lastPDM); smoothedMinusDM.push(lastMDM);
            }
            if (i >= 14 && lastTR > 0) {
               const pdi = 100 * (lastPDM / lastTR);
               const mdi = 100 * (lastMDM / lastTR);
               const curDX = 100 * Math.abs(pdi - mdi) / (pdi + mdi || 1);
               dx.push(curDX);
               if (i === 27) {
                 lastADX = dx.slice(14, 28).reduce((a,b)=>a+b,0)/14;
                 adxData.push(lastADX);
               } else if (i > 27) {
                 lastADX = ((lastADX * 13) + curDX) / 14;
                 adxData.push(lastADX);
               } else { adxData.push(null); }
            } else { dx.push(0); adxData.push(null); }
        }
        cObj.adxSeries.setData(adxData.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time));
    } else { cObj.adxSeries.setData([]); }
"""

text = re.sub(r'assignOscillator\(cObj\.rsiSeries.*?assignOscillator\(cObj\.atrSeries.*?\);', new_oscillators.strip(), text, flags=re.DOTALL)

with open("app/js/multi-chart.js", "w") as f:
    f.write(text)

