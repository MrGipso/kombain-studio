import {AbsoluteFill, Img, spring, useCurrentFrame, useVideoConfig} from 'remotion';

export const ProductReveal: React.FC<{
  image?: string; title?: string; subtitle?: string; price?: string;
  features?: string[]; badge?: string; badgeColor?: string;
  backgroundColor?: string; accentColor?: string;
}> = ({image, title, subtitle, price, features, badge, badgeColor = '#EF4444', backgroundColor = '#0F172A', accentColor = '#7C3AED'}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 14}});
  const pImg = spring({frame: frame + 5, fps, config: {damping: 16}});
  const pDetails = spring({frame: frame + 10, fps, config: {damping: 16}});

  return (
    <AbsoluteFill style={{display: 'flex', flexDirection: 'row', background: backgroundColor, padding: 80, gap: 60, alignItems: 'center'}}>
      <div style={{flex: 1, display: 'flex', justifyContent: 'center', alignItems: 'center'}}>
        {image ? (
          <div style={{
            width: 400, height: 500, borderRadius: 24,
            background: 'linear-gradient(135deg, #1E293B, #334155)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            opacity: pImg, transform: `translateX(${(1 - pImg) * -40}px) rotateY(${(1 - pImg) * 20}deg) perspective(1000px)`,
            boxShadow: `0 30px 60px rgba(0,0,0,0.4)`,
          }}>
            <Img src={image} style={{width: '80%', height: '80%', objectFit: 'contain'}} />
          </div>
        ) : (
          <div style={{width: 400, height: 500, borderRadius: 24, background: 'linear-gradient(135deg, #1E293B, #334155)', display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: p}}>
            <div style={{fontSize: 80, color: '#64748B'}}>📦</div>
          </div>
        )}
      </div>
      <div style={{flex: 1, opacity: pDetails, transform: `translateX(${(1 - pDetails) * 40}px)`}}>
        {badge && <div style={{display: 'inline-block', background: badgeColor, color: '#FFFFFF', padding: '6px 16px', borderRadius: 20, fontSize: 14, fontWeight: 600, fontFamily: 'Inter, sans-serif', marginBottom: 16}}>{badge}</div>}
        {title && <div style={{fontSize: 48, fontWeight: 800, color: '#F8FAFC', fontFamily: 'Inter, sans-serif', marginBottom: 12}}>{title}</div>}
        {subtitle && <div style={{fontSize: 22, color: '#94A3B8', fontFamily: 'Inter, sans-serif', marginBottom: 16}}>{subtitle}</div>}
        {price && <div style={{fontSize: 40, fontWeight: 800, color: accentColor, fontFamily: 'Inter, sans-serif', marginBottom: 24}}>{price}</div>}
        {features && features.map((f, i) => (
          <div key={i} style={{display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8, opacity: spring({frame: frame + 15 + i * 2, fps, config: {damping: 14}})}}>
            <div style={{width: 8, height: 8, borderRadius: '50%', background: accentColor}} />
            <div style={{fontSize: 18, color: '#CBD5E1', fontFamily: 'Inter, sans-serif'}}>{f}</div>
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
