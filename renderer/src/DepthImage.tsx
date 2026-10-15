import React, {useEffect, useLayoutEffect, useRef, useState} from 'react';
import {cancelRender, continueRender, delayRender, staticFile} from 'remotion';

// 2.5D camera: the scene image drawn through its depth map (white = near). Pixels nearer than the subject move further
// with the camera and grow faster on a push; pixels behind it move less. The subject (depth at `focus`) stays put.
// Offsets are small, so stretched edges stay invisible and no gap filling is needed.
const VERT = `attribute vec2 p; varying vec2 uv;
void main(){ uv = vec2(p.x * .5 + .5, .5 - p.y * .5); gl_Position = vec4(p, 0., 1.); }`;
const FRAG = `precision highp float; varying vec2 uv;
uniform sampler2D img; uniform sampler2D dep; uniform vec2 focus; uniform vec2 shift; uniform float grow;
void main(){
  float d = texture2D(dep, uv).r - texture2D(dep, focus).r;   // > 0 nearer than the subject
  vec2 q = uv - shift * d;
  q = focus + (q - focus) / (1. + grow * d);
  gl_FragColor = texture2D(img, clamp(q, 0., 1.));
}`;

const load = (src: string) => new Promise<HTMLImageElement>((resolve, reject) => {
  const image = new Image();
  image.onload = () => resolve(image);
  image.onerror = () => reject(new Error('Could not load ' + src));
  image.src = staticFile(src);
});

export const DepthImage: React.FC<{src: string; depth: string; width: number; height: number;
  focus: {x: number; y: number}; shift: [number, number]; grow: number; style: React.CSSProperties}> =
  ({src, depth, width, height, focus, shift, grow, style}) => {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [gl, setGl] = useState<{context: WebGLRenderingContext; program: WebGLProgram} | null>(null);
  const [handle] = useState(() => delayRender('Depth image ' + src));
  const released = useRef(false);
  useEffect(() => {
    Promise.all([load(src), load(depth)]).then(([image, map]) => {
      const context = canvas.current!.getContext('webgl', {preserveDrawingBuffer: true, antialias: false});
      if (!context) throw new Error('WebGL is unavailable');
      const shader = (type: number, source: string) => {
        const s = context.createShader(type)!; context.shaderSource(s, source); context.compileShader(s);
        if (!context.getShaderParameter(s, context.COMPILE_STATUS)) throw new Error(context.getShaderInfoLog(s) || 'shader');
        return s;
      };
      const program = context.createProgram()!;
      context.attachShader(program, shader(context.VERTEX_SHADER, VERT));
      context.attachShader(program, shader(context.FRAGMENT_SHADER, FRAG));
      context.linkProgram(program); context.useProgram(program);
      const buffer = context.createBuffer(); context.bindBuffer(context.ARRAY_BUFFER, buffer);
      context.bufferData(context.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), context.STATIC_DRAW);
      const at = context.getAttribLocation(program, 'p');
      context.enableVertexAttribArray(at); context.vertexAttribPointer(at, 2, context.FLOAT, false, 0, 0);
      [image, map].forEach((source, unit) => {
        context.activeTexture(context.TEXTURE0 + unit);
        context.bindTexture(context.TEXTURE_2D, context.createTexture());
        context.texParameteri(context.TEXTURE_2D, context.TEXTURE_MIN_FILTER, context.LINEAR);
        context.texParameteri(context.TEXTURE_2D, context.TEXTURE_MAG_FILTER, context.LINEAR);
        context.texParameteri(context.TEXTURE_2D, context.TEXTURE_WRAP_S, context.CLAMP_TO_EDGE);
        context.texParameteri(context.TEXTURE_2D, context.TEXTURE_WRAP_T, context.CLAMP_TO_EDGE);
        context.texImage2D(context.TEXTURE_2D, 0, context.RGBA, context.RGBA, context.UNSIGNED_BYTE, source);
      });
      context.uniform1i(context.getUniformLocation(program, 'img'), 0);
      context.uniform1i(context.getUniformLocation(program, 'dep'), 1);
      setGl({context, program});
    }).catch(cancelRender);
  }, [src, depth]);
  useLayoutEffect(() => {
    if (!gl) return;
    const {context, program} = gl;
    context.viewport(0, 0, width, height);
    context.uniform2f(context.getUniformLocation(program, 'focus'), focus.x / 100, focus.y / 100);
    context.uniform2f(context.getUniformLocation(program, 'shift'), shift[0], shift[1]);
    context.uniform1f(context.getUniformLocation(program, 'grow'), grow);
    context.drawArrays(context.TRIANGLE_STRIP, 0, 4);
    if (!released.current) { released.current = true; continueRender(handle); }
  }, [gl, width, height, focus.x, focus.y, shift[0], shift[1], grow, handle]);
  return <canvas ref={canvas} width={width} height={height} style={style}/>;
};
