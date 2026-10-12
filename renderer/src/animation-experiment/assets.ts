/** Opt-in SVG tinting; fixed-color artwork keeps its original bytes. */
export const assetImageSource=(asset:{data_uri:string;capabilities?:string[]},color?:string):string=>{
  const prefix='data:image/svg+xml;base64,';
  if(!asset.capabilities?.includes('renderer-recolorable stroke') || !color ||
     !/^#[0-9a-fA-F]{6}$/.test(color) || !asset.data_uri.startsWith(prefix))return asset.data_uri;
  const svg=atob(asset.data_uri.slice(prefix.length));
  return prefix+btoa(svg.replace(/(["'])currentColor\1/g,(_,quote:string)=>quote+color+quote));
};
