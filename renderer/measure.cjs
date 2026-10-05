const fs=require('node:fs');
const path=require('node:path');
const {createRequire}=require('node:module');

async function main(){
  const [snapshot,propsFile,browserExecutable,output]=process.argv.slice(2);
  fs.mkdirSync(output,{recursive:true});
  const fromSnapshot=createRequire(path.join(snapshot,'package.json'));
  const {bundle}=fromSnapshot('@remotion/bundler');
  const {openBrowser,selectComposition,renderStill}=fromSnapshot('@remotion/renderer');
  const inputProps=JSON.parse(fs.readFileSync(propsFile,'utf8'));
  const serveUrl=await bundle({entryPoint:path.join(snapshot,'src/index.ts'),publicDir:path.join(snapshot,'public'),outDir:path.join(output,'bundle')});
  const browser=await openBrowser('chrome',{browserExecutable});
  const results=[];
  try{
    const composition=await selectComposition({serveUrl,id:'InfoFilm',inputProps,puppeteerInstance:browser});
    for(const scene of inputProps.data.scenes){
      for(const fraction of [.18,.82]){
        const frame=Math.min(scene.from+scene.duration-1,scene.from+Math.floor(scene.duration*fraction));
        const file=scene.id+'-'+Math.round(fraction*100)+'.png';
        const logs=[];
        await renderStill({serveUrl,composition,inputProps,puppeteerInstance:browser,
          output:path.join(output,file),frame,imageFormat:'png',overwrite:true,
          onBrowserLog:log=>logs.push(log.text),logLevel:'error'});
        const records=logs.filter(x=>x.startsWith('INFOFILM_LAYOUT ')).map(x=>JSON.parse(x.slice('INFOFILM_LAYOUT '.length))).filter(x=>x.frame===frame);
        if(!records.length)throw new Error('Missing browser measurements at '+frame);
        const measured=records[records.length-1];
        const failures=[];
        for(const box of measured.boxes){
          if(box.x<-.5||box.y<-.5||box.x+box.width>measured.width+.5||box.y+box.height>measured.height+.5)failures.push({reason:'outside frame',box});
          if(box.scrollWidth>box.clientWidth+2||box.scrollHeight>box.clientHeight+2)failures.push({reason:'text overflow',box});
        }
        // Planned text groups must not collide with the actual caption box.
        const captions=measured.boxes.filter(x=>x.role==='caption');
        for(const tag of measured.boxes.filter(x=>x.role==='expert-review-tag')){
          for(const other of measured.boxes.filter(x=>['series-label','context-banner'].includes(x.role))){
            if(tag.x<other.x+other.width && tag.x+tag.width>other.x && tag.y<other.y+other.height && tag.y+tag.height>other.y)
              failures.push({reason:'expert review tag overlaps header',box:tag,other});
          }
        }
        for(const box of measured.boxes.filter(x=>['heading','summary','diagram-card'].includes(x.role))){
          for(const caption of captions){
            if(box.x<caption.x+caption.width && box.x+box.width>caption.x && box.y<caption.y+caption.height && box.y+box.height>caption.y)
              failures.push({reason:'caption overlaps planned content',box,caption});
          }
        }
        results.push({scene_id:scene.id,frame,file,measured,failures});
        process.stdout.write(JSON.stringify({scene:scene.id,frame,failures:failures.length})+'\n');
      }
    }
    fs.writeFileSync(path.join(output,'layout-results.json'),JSON.stringify({passed:results.every(x=>!x.failures.length),samples:results},null,2));
  }finally{await browser.close({silent:true});}
}
main().catch(error=>{process.stderr.write(String(error.stack||error)+'\n');process.exitCode=1;});
