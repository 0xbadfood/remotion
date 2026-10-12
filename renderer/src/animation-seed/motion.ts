export type Physics = {gravity_m_s2:number; release_height_m:number; launch_speed_m_s:number; drop_x_m:number};

export const landingTime = (p:Physics) => Math.sqrt(2*p.release_height_m/p.gravity_m_s2);
export const position = (p:Physics, time:number, dropped=false) => {
  const t=Math.max(0, Math.min(time, landingTime(p)));
  return {x:dropped?p.drop_x_m:p.launch_speed_m_s*t,
    y:Math.max(0,p.release_height_m-p.gravity_m_s2*t*t/2),
    vx:dropped?0:p.launch_speed_m_s, vy:-p.gravity_m_s2*t};
};
export const presentationTime = (seconds:number, start:number, end:number, p:Physics) => {
  return Math.max(0,Math.min(1,(seconds-start)/(end-start)))*landingTime(p);
};
