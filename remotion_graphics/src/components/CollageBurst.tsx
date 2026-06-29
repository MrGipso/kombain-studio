import {AbsoluteFill, Img, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

export const CollageBurst: React.FC<{
  images: string[]; title?: string; layout?: 'grid' | 'masonry' | 'burst';
  backgroundColor?: string;
}> = ({images, title, layout = 'burst', backgroundColor = '#0F172A'}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();

  const positions = layout === 'grid' ? images.map((_, i) => {
    const cols = Math.min(images.length, 3);
    const col = i % cols;
    const row = Math.floor(i / cols);
    return {x: 200 + col * 520, y: 200 + row * 360, r: 0, s: 1};
  }) : layout === 'masonry' ? images.map((_, i) => {
    const cols = Math.min(images.length, 4);
    const col = i % cols;
    const row = Math.floor(i / cols);
    const h = [400, 320, 360, 280][i % 4];
    return {x: 80 + col * 450, y: row * 360 + 100, r: 4, s: 1};
  }) : images.map((_, i) => {
    const angle = (i / images.length) * 360 - 90;
    const dist = 300 + i * 20;
    const rad = (angle * Math.PI) / 180;
    return {x: 960 + Math.cos(rad) * dist, y: 540 + Math.sin(rad) * dist, r: angle + 90, s: 0.6 + (i % 3) * 0.2};
  });

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor, overflow: 'hidden'}}>
      {title && (
        <div style={{position: 'absolute', top: 60, zIndex: 10, fontSize: 44, fontWeight: 700, color: '#F8FAFC', fontFamily: 'Inter, sans-serif', textShadow: '0 4px 20px rgba(0,0,0,0.6)', opacity: spring({frame: frame - 10, fps, config: {damping: 16}})}}>
          {title}
        </div>
      )}
      {images.slice(0, Math.min(images.length, 8)).map((src, i) => {
        const pos = positions[i];
        const d = spring({frame: frame - i * 3, fps, config: {damping: 12}});
        const x = interpolate(d, [0, 1], [960, pos.x]);
        const y = interpolate(d, [0, 1], [540, pos.y]);
        const s = interpolate(d, [0, 1], [0.1, pos.s]);
        const r = interpolate(d, [0, 1], [540, pos.r]);
        const fadeOut = interpolate(frame, [durationInFrames - 10, durationInFrames], [1, 0]);
        return (
          <div key={i} style={{
            position: 'absolute', width: 280, height: 200, borderRadius: 12,
            overflow: 'hidden', boxShadow: '0 10px 30px rgba(0,0,0,0.4)',
            transform: `translate(${x - 140}px, ${y - 100}px) rotate(${r}deg) scale(${s})`,
            opacity: d * fadeOut,
          }}>
            <Img src={src} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
