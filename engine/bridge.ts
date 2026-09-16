/** Loopback transport for BottleShip's supported game automation harness. */
import { connect, pageEval } from '../.runtime/bottleship/tools/cdp-core';

const port = Number(process.env.TSAI_BRIDGE_PORT || 3917);
const cdpPort = Number(process.env.TSAI_CDP_PORT || 9333);
let session: any;
async function connected() {
  if (!session) session = (await connect({port:cdpPort})).session;
  return session;
}
class BadRequest extends Error {}
function memoryRange(addr: unknown, len: unknown) {
  return typeof addr === 'number' && typeof len === 'number' && Number.isInteger(addr) && addr >= 0 && addr <= 0xffffffff && Number.isInteger(len) && len >= 1 && len <= 65536 && addr + len <= 0x100000000;
}
const permitted = new Set(['ping','readBytes','state','report','shot','pause','resume','tickFrames','clickAt','clickHold','move','drag','key','keyHold','type','sleep','fsList','fsRead']);
async function rpc(cmd: string, args: unknown[] = []) {
  if (!permitted.has(cmd)) throw new BadRequest('Unsupported game harness command');
  if (cmd === 'readBytes' && (args.length !== 2 || !memoryRange(args[0],args[1]))) throw new BadRequest('Invalid memory range');
  if (cmd === 'sleep' && (args.length !== 1 || typeof args[0] !== 'number' || !Number.isFinite(args[0]) || args[0] < 0 || args[0] > 30000)) throw new BadRequest('Invalid sleep interval');
  return await pageEval(await connected(), `window.__BS__.harness.rpc(...[${JSON.stringify(cmd)},${JSON.stringify(args)},{timeoutMs:30000}])`, {timeoutMs:35000});
}
function json(body: unknown, status = 200) { return Response.json(body,{status}); }
Bun.serve({
  hostname:'127.0.0.1', port,
  idleTimeout:60,
  async fetch(req) {
    const url = new URL(req.url);
    if (Number(req.headers.get('content-length') || 0) > 65536) return json({error:'Request body is too large'},413);
    if (req.headers.has('origin')) return json({error:'Browser-origin requests are disabled'},403);
    try {
      if (req.method === 'GET' && url.pathname === '/health') return json(await rpc('ping'));
      if (req.method === 'POST' && url.pathname === '/boot') {
        const s = await connected();
        await s.send('Page.reload',{ignoreCache:true});
        await Bun.sleep(500); // Allow the old execution context to be destroyed.
        let mounted = false;
        for (let n=0;n<100;n++) {
          const ready = await pageEval(s,'!!(window.__BS__?.harness && window.loadApp)').catch(()=>false);
          if (ready) { mounted = true; break; }
          await Bun.sleep(100);
        }
        if (!mounted) throw new Error('Game harness did not mount');
        return json(await pageEval(s, 'window.__BS__.harness.openWgb("http://localhost:5174/starcraft_demo.wgb")', {timeoutMs:30000}));
      }

      if (req.method === 'GET' && url.pathname === '/memory') {
        const addr = Number(url.searchParams.get('addr'));
        const len = Number(url.searchParams.get('len') || 64);
        if (!url.searchParams.has('addr') || !memoryRange(addr,len)) return json({error:'Invalid memory range'},400);
        return json(await rpc('readBytes',[addr,len]));
      }
      if (req.method === 'GET' && url.pathname === '/screenshot') {
        const s = await connected();
        // Match BottleShip gridShot's canvas clipping: the paused DDraw presenter
        // can return a black worker-side capture despite a valid composed frame.
        const clip = await pageEval(s, `(() => { const c = document.querySelector('canvas'); if (!c) throw Error('No game canvas'); const r = c.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,scale:1}; })()`);
        const shot = await s.send('Page.captureScreenshot',{format:'png',clip});
        return new Response(Buffer.from(shot.result.data,'base64'), {headers:{'Content-Type':'image/png','Cache-Control':'no-store'}});
      }
      if (req.method === 'POST' && url.pathname === '/rpc') {
        const {cmd,args=[]} = await req.json() as {cmd:string,args:unknown[]};
        if (!Array.isArray(args)) return json({error:'args must be an array'},400);
        return json(await rpc(cmd,args));
      }
      if (req.method === 'POST' && url.pathname === '/step') {
        const {frames=24} = await req.json() as {frames:number};
        if (!Number.isInteger(frames) || frames < 1 || frames > 240) return json({error:'frames must be 1..240'},400);
        return json(await rpc('tickFrames',[frames,{park:true}]));
      }
      return json({error:'Not found'},404);
    } catch(e) {
      if (e instanceof BadRequest || e instanceof SyntaxError) return json({error:'Invalid game harness request'},400);
      session?.close(); session=undefined;
      return json({error:'Game harness request failed'},500);
    }
  }
});
console.log(`BottleShip bridge listening on http://127.0.0.1:${port}`);
