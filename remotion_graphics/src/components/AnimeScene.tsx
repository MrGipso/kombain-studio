import {AbsoluteFill, Img, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

export const AnimeScene: React.FC<{
  image?: string; text?: string; subtitle?: string;
  effect?: 'speed' | 'impact' | 'reveal' | 'zoom';
  textColor?: string; backgroundColor?: string;
}> = ({image, text, subtitle, effect = 'reveal', textColor = '#FFFFFF', backgroundColor = '#0F172A'}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();

  const p = spring({frame, fps, config: {damping: 12, stiffness: 50}});
  const shift = interpolate(frame, [0, durationInFrames], [0, -30]);

  const lines = effect === 'speed' ? Array.from({length: 12}).map((_, i) => {
    const x = 960 + Math.cos((i / 12) * Math.PI * 2) * (800 + shift * 2);
    const y = 540 + Math.sin((i / 12) * Math.PI * 2) * (800 + shift * 2);
    return <line key={i} x1={960} y1={540} x2={x} y2={y} stroke="#F59E0B" strokeWidth={2} opacity={0.3 * p} />;
  }) : null;

  const impactOpacity = effect === 'impact' ? interpolate(frame, [0, 10, 20, durationInFrames], [0, 1, 0.8, 0]) : p;

  return (
    <AbsoluteFill style={{background: backgroundColor, overflow: 'hidden'}}>
      {image && <Img src={image} style={{width: '100%', height: '100%', objectFit: 'cover', opacity: effect === 'zoom' ? interpolate(frame, [0, durationInFrames], [1, 1.3]) : p, transform: `scale(${effect === 'zoom' ? 1 + (frame / durationInFrames) * 0.3 : 1})`}} />}
      {effect === 'impact' && <div style={{position: 'absolute', inset: 0, background: `radial-gradient(circle at 960px 540px, rgba(245,158,11,${0.4 * (1 - frame / durationInFrames)}), transparent 60%)`}} />}
      <svg viewBox="0 0 1920 1080" style={{position: 'absolute', width: '100%', height: '100%'}}>
        {lines}
      </svg>
      <div style={{position: 'absolute', bottom: 120, left: 0, right: 0, textAlign: 'center', opacity: impactOpacity, transform: `translateY(${(1 - p) * 50}px)`}}>
        {text && <div style={{fontSize: 56, fontWeight: 800, color: textColor, fontFamily: 'Inter, sans-serif', textShadow: '0 4px 20px rgba(0,0,0,0.8)'}}>{text}</div>}
        {subtitle && <div style={{fontSize: 28, color: '#CBD5E1', fontFamily: 'Inter, sans-serif', marginTop: 12, textShadow: '0 2px 10px rgba(0,0,0,0.6)'}}>{subtitle}</div>}
      </div>
    </AbsoluteFill>
  );
};
