const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {test}=require('node:test'),ts=require('typescript');
const source=ts.transpileModule(fs.readFileSync(path.join(__dirname,'assets.ts'),'utf8'),{
 compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
const mod={exports:{}};new Function('exports','module',source)(mod.exports,mod);
const {assetImageSource}=mod.exports;
const svg='<svg><path stroke="currentColor" fill="currentColor"/><path fill="#ffffff"/></svg>';
const uri='data:image/svg+xml;base64,'+btoa(svg);
const asset={data_uri:uri,capabilities:['renderer-recolorable stroke']};
test('tints only explicit placeholders and keeps fixed artwork colors',()=>{
 const changed=atob(assetImageSource(asset,'#38BDF8').split(',')[1]);
 assert.equal(changed,'<svg><path stroke="#38BDF8" fill="#38BDF8"/><path fill="#ffffff"/></svg>');
 assert.equal(asset.data_uri,uri);
});
test('existing fixed-color assets are unchanged',()=>assert.equal(assetImageSource({data_uri:uri},'#38BDF8'),uri));
test('invalid or absent color cannot modify SVG',()=>{
 for(const color of [undefined,'red','"><script/>'])assert.equal(assetImageSource(asset,color),uri);
});
