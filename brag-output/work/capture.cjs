const { chromium } = require('/Users/dhruv/dev/smart-thermostat-with-vents/e2e/node_modules/playwright');
const path = require('path'); const fs = require('fs'); const { spawn } = require('child_process');
(async () => {
  const mode = process.argv[2]; // "stills" or "video"
  const browser = await chromium.launch({ executablePath: process.env.CHROME });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  await page.goto('file://' + path.resolve('video.html'));
  await page.evaluate(() => document.fonts.ready);
  if (mode === 'stills') {
    fs.mkdirSync('stills', { recursive: true });
    const ts = process.argv.slice(3).map(Number);
    for (const t of ts) { await page.evaluate((t) => render(t), t); await page.screenshot({ path: `stills/t${t.toFixed(2)}.png` }); }
  } else {
    const FPS = 30, DUR = 21, N = FPS * DUR;
    const ff = spawn(process.argv[3], ['-y','-f','image2pipe','-framerate',String(FPS),'-c:v','png','-i','-','-c:v','libx264','-pix_fmt','yuv420p','-crf','16','-preset','slow','silent.mp4'], { stdio: ['pipe','ignore','inherit'] });
    for (let i = 0; i < N; i++) {
      await page.evaluate((t) => render(t), i / FPS);
      const buf = await page.screenshot({ type: 'png' });
      if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    }
    ff.stdin.end(); await new Promise(r => ff.on('close', r));
  }
  await browser.close();
})();
