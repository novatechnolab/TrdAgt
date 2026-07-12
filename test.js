const puppeteer = require('puppeteer');
(async () => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:5000');
  await page.click('[data-page="multi-chart"]');
  await page.waitForTimeout(500);
  
  // Type stock
  await page.type('#mc-stock-select', 'TCS');
  await page.click('#mc-display-chart-btn');
  await page.waitForTimeout(1500);
  
  const heights = await page.evaluate(() => {
     const box = document.querySelector('.mc-chart-box');
     const container = document.querySelector('.mc-chart-container');
     const tv = document.querySelector('.tv-lightweight-charts');
     const canvas = document.querySelector('.tv-lightweight-charts canvas');
     const header = document.querySelector('.mc-chart-header');
     return {
       boxH: box?.clientHeight,
       headerH: header?.offsetHeight,
       containerH: container?.clientHeight,
       tvH: tv?.clientHeight,
       canvasH: canvas?.height,
     };
  });
  console.log(JSON.stringify(heights));
  await browser.close();
})();
