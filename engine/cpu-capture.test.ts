import { expect, test } from 'bun:test';
import { runInNewContext } from 'node:vm';
import { CPU_FRAME_EXPRESSION, CpuCapture, encodePaletteFrame, type PaletteFrame } from './cpu-capture';

function fixture(pitch=640): PaletteFrame {
  const pixels=Buffer.alloc(pitch*480,99);
  for (let y=0;y<480;y++) pixels.fill(y%2 ? 2 : 1,y*pitch,y*pitch+640);
  const palette=Array(256).fill(0xff000000);
  palette[1]=0xff030201; palette[2]=0xff060504;
  return {width:640,height:480,pitch,palette,indices:pixels.toString('base64')};
}

test('PNG verifies CRCs and preserves original colors, row stride, and dimensions', () => {
  const png=encodePaletteFrame(fixture(672));
  const result=Bun.spawnSync(['python3','-c',`
import io,json,sys
from PIL import Image
data=sys.stdin.buffer.read()
Image.open(io.BytesIO(data)).verify()
im=Image.open(io.BytesIO(data)).convert('RGB')
print(json.dumps([list(im.size),list(im.getpixel((0,0))),list(im.getpixel((639,1))),list(im.getpixel((639,479)))]))
`],{stdin:png});
  expect(result.exitCode).toBe(0);
  expect(JSON.parse(result.stdout.toString())).toEqual([[640,480],[1,2,3],[4,5,6],[4,5,6]]);
});

test('encoding rejects wrong sizes, truncated pixels, and malformed palettes', () => {
  for (const change of [{width:641},{height:481},{pitch:639},{pitch:4097},{pitch:640.5},
    {palette:Array(255).fill(0)},{palette:Array(256).fill(-1)},
    {palette:Array(256).fill(0x100000000)},{indices:'not base64'},
    {indices:fixture().indices.slice(4)}]) {
    expect(()=>encodePaletteFrame({...fixture(),...change})).toThrow();
  }
});

function snapshotContext() {
  const frame=fixture();
  const memory=Buffer.concat([Buffer.alloc(16),Buffer.from(frame.indices,'base64')]);
  const surface={surfacePtr:16,width:640,height:480,pitch:640,format:{bpp:8},paletteHandle:7,mode:'CPU'};
  let snapshots=0;
  const dd={getFrameSnapshot:()=>({lastPresent:{surfaceAddr:16}}),context:{resourceProvider:{
    getAllComObjects:()=>[{getState:()=>surface}],
  }}};
  const system={process:{getModule:()=>dd,getCurrentMemory:()=>({length:memory.length,
    slice:(start:number,end:number)=>{snapshots++;return memory.subarray(start,end);}})},
    resourceProvider:{getComObject:(handle:number)=>handle===7 ? {getEntries:()=>frame.palette} : undefined}};
  return {frame,surface,dd,context:{System:{getInstance:()=>system},btoa:(text:string)=>Buffer.from(text,'binary').toString('base64')},snapshots:()=>snapshots};
}

test('worker snapshot reads only the presented original CPU surface and attached palette', () => {
  const f=snapshotContext();
  const result=runInNewContext(CPU_FRAME_EXPRESSION,f.context);
  expect(result).toEqual(f.frame);
  expect(f.snapshots()).toBe(1);
  // A stale GPU-only surface must never silently become a CPU screenshot.
  f.surface.mode='GPU';
  expect(()=>runInNewContext(CPU_FRAME_EXPRESSION,f.context)).toThrow();
  expect(f.snapshots()).toBe(1);
});

test('worker snapshot refuses missing palette, ambiguous surfaces, and out-of-bounds memory', () => {
  let f=snapshotContext(); f.surface.paletteHandle=8;
  expect(()=>runInNewContext(CPU_FRAME_EXPRESSION,f.context)).toThrow();
  f=snapshotContext(); f.surface.pitch=4096;
  expect(()=>runInNewContext(CPU_FRAME_EXPRESSION,f.context)).toThrow();
  f=snapshotContext(); f.dd.getFrameSnapshot=()=>({lastPresent:{surfaceAddr:99}});
  f.dd.context.resourceProvider.getAllComObjects=()=>[{getState:()=>f.surface},{getState:()=>({...f.surface,surfacePtr:32})}];
  expect(()=>runInNewContext(CPU_FRAME_EXPRESSION,f.context)).toThrow();
});

test('capture reuses one attachment and submits only the fixed read-only expression', async () => {
  const calls:any[]=[];
  const session={send:async (...args:any[])=>{
    calls.push(args);
    if(args[0]==='Target.getTargets') return {result:{targetInfos:[{targetId:'game-worker',type:'worker',url:'http://localhost:5174/src/worker/emulator.worker.ts?worker_file&type=module'}]}};
    if(args[0]==='Target.attachToTarget') return {result:{sessionId:'worker-session'}};
    return {result:{result:{value:fixture()}}};
  }};
  const capture=new CpuCapture();
  await capture.capture(session); await capture.capture(session);
  expect(calls.filter(call=>call[0]==='Target.attachToTarget')).toHaveLength(1);
  const evaluations=calls.filter(call=>call[0]==='Runtime.evaluate');
  expect(evaluations).toHaveLength(2);
  for(const call of evaluations) {
    expect(call[1]).toEqual({expression:CPU_FRAME_EXPRESSION,returnByValue:true,awaitPromise:false});
    expect(call[2]).toBe('worker-session');
  }
});
