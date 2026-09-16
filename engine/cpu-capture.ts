/** Read-only capture of StarCraft's original paletted DirectDraw framebuffer. */
import { deflateSync } from 'node:zlib';

// Fixed expression: callers cannot choose an address or evaluate arbitrary code.
// Runs synchronously in the emulator worker, so palette and pixels are copied
// together without a guest task running between the two reads.
export const CPU_FRAME_EXPRESSION = `(() => {
  const system = globalThis.System?.getInstance();
  const dd = system?.process?.getModule('ddraw');
  if (!dd?.context) throw Error('DirectDraw is unavailable');
  const surfaces = Array.from(dd.context.resourceProvider.getAllComObjects())
    .map(object => object.getState?.()).filter(surface => surface?.surfacePtr
      && surface.width === 640 && surface.height === 480 && surface.format?.bpp === 8);
  const last = dd.getFrameSnapshot?.()?.lastPresent?.surfaceAddr;
  const surface = surfaces.find(surface => surface.surfacePtr === last)
    ?? (surfaces.length === 1 ? surfaces[0] : null);
  if (!surface || surface.mode !== 'CPU' || surface.pitch < 640 || surface.pitch > 4096)
    throw Error('No unambiguous CPU gameplay surface');
  const palette = system.resourceProvider.getComObject(surface.paletteHandle)?.getEntries?.();
  if (!palette || palette.length !== 256) throw Error('Original game palette is unavailable');
  const memory = system.process.getCurrentMemory();
  const start = surface.surfacePtr >>> 0;
  const size = surface.pitch * 480;
  if (start + size > memory.length) throw Error('Gameplay surface is outside guest memory');
  const pixels = memory.slice(start, start + size);
  let binary = '';
  for (let i = 0; i < pixels.length; i += 8192)
    binary += String.fromCharCode(...pixels.subarray(i, i + 8192));
  return {width:640,height:480,pitch:surface.pitch,palette:Array.from(palette),indices:btoa(binary)};
})()`;

export type PaletteFrame = {width:number; height:number; pitch:number; palette:number[]; indices:string};

function crc32(bytes: Buffer): number {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit=0; bit<8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(type: string, data: Buffer): Buffer {
  const payload = Buffer.concat([Buffer.from(type,'ascii'),data]);
  const length = Buffer.alloc(4); length.writeUInt32BE(data.length);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(payload));
  return Buffer.concat([length,payload,crc]);
}

export function encodePaletteFrame(frame: PaletteFrame): Buffer {
  if (frame?.width !== 640 || frame?.height !== 480 || !Number.isInteger(frame.pitch)
      || frame.pitch < 640 || frame.pitch > 4096 || !Array.isArray(frame.palette)
      || frame.palette.length !== 256 || frame.palette.some(value => !Number.isInteger(value)
        || value < 0 || value > 0xffffffff) || typeof frame.indices !== 'string'
      || frame.indices.length !== Math.ceil(frame.pitch * 480 / 3) * 4
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(frame.indices)) throw Error('Invalid CPU gameplay frame');
  const pixels = Buffer.from(frame.indices,'base64');
  if (pixels.length !== frame.pitch * 480) throw Error('Incomplete CPU gameplay frame');
  const rows = Buffer.alloc(641 * 480); // PNG filter zero followed by original indices.
  for (let y=0; y<480; y++) pixels.copy(rows,y*641+1,y*frame.pitch,y*frame.pitch+640);
  const palette = Buffer.alloc(256*3);
  for (let i=0; i<256; i++) {
    const color = frame.palette[i]; // Runtime palette is little-endian RGBA.
    palette[i*3] = color & 255; palette[i*3+1] = (color >>> 8) & 255;
    palette[i*3+2] = (color >>> 16) & 255;
  }
  const header=Buffer.alloc(13); header.writeUInt32BE(640); header.writeUInt32BE(480,4);
  header[8]=8; header[9]=3; // Original 8-bit indexed colors, opaque output.
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),
    chunk('PLTE',palette),chunk('IDAT',deflateSync(rows)),chunk('IEND',Buffer.alloc(0))]);
}

/** Cache one worker attachment instead of accumulating per-frame CDP listeners. */
export class CpuCapture {
  private workerSession?: string;
  reset() { this.workerSession = undefined; }
  async capture(session: any): Promise<Buffer> {
    if (!this.workerSession) {
      // Follow BottleShip's worker transport initialization explicitly, so the
      // first capture does not depend on an earlier diagnostic attachment.
      await session.send('Target.setDiscoverTargets',{discover:true});
      const targets = await session.send('Target.getTargets');
      const candidates = (targets.result?.targetInfos ?? []).filter((target: any) =>
        target.type === 'worker' && target.url.startsWith('http://localhost:5174/src/worker/emulator.worker.ts?'));
      if (candidates.length !== 1) throw Error('Emulator worker is ambiguous or unavailable');
      const attached = await session.send('Target.attachToTarget',{targetId:candidates[0].targetId,flatten:true});
      this.workerSession = attached.result?.sessionId;
      if (!this.workerSession) throw Error('Could not attach to emulator worker');
      await session.send('Runtime.enable',{},this.workerSession);
    }
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const result: any = await Promise.race([
        session.send('Runtime.evaluate',{expression:CPU_FRAME_EXPRESSION,returnByValue:true,awaitPromise:false},this.workerSession),
        new Promise((_resolve,reject) => { timeout=setTimeout(()=>reject(Error('CPU capture timed out')),10000); }),
      ]);
      if (result.result?.exceptionDetails) throw Error('CPU gameplay capture failed');
      return encodePaletteFrame(result.result?.result?.value);
    } catch (error) { this.reset(); throw error; }
    finally { if (timeout) clearTimeout(timeout); }
  }
}
