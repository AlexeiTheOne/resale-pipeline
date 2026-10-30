// Screenshot the site in a few states. Usage: node shots.mjs <outdir> [mobile]
import { chromium } from 'playwright'
const out = process.argv[2]
const mobile = process.argv[3] === 'mobile'
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const page = await browser.newPage(mobile
  ? { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }
  : { viewport: { width: 1440, height: 900 } })
const logs = []
page.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') logs.push(m.text()) })
page.on('pageerror', e => logs.push('PAGEERROR ' + e.message))
const tag = mobile ? 'm' : 'd'
const snap = async (name, wait = 2500) => { await page.waitForTimeout(wait); await page.screenshot({ path: `${out}/${tag}-${name}.png` }) }

await page.goto('http://localhost:5173/')
await snap('1-house', 4000)
await page.evaluate(() => { location.hash = 'G' })
await snap('2-room', 3000)
await page.evaluate(() => { location.hash = 'G7' })
await snap('3-box', 4500)
const first = page.locator('.pcard').first()
if (await first.count()) { await first.click({ force: true }); await snap('4-piece', 2000) }
await page.locator('.crumbs .back').click(); await page.waitForTimeout(400); await page.locator('.crumbs .back').click(); await snap('4b-back', 2500)
await page.evaluate(() => { location.hash = '' ; })
await page.locator('.crumbs button', { hasText: 'House' }).click()
await page.fill('.search input', 'oakley')
await snap('5-search', 2500)
console.log(logs.slice(0, 15).join('\n'))
await browser.close()
