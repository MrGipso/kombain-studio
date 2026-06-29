import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface LyricLine {
  words: {text: string; startFrame: number; endFrame: number}[];
}

export const LyricOverlay: React.FC<{
  lyrics: LyricLine[]; backgroundColor?: string;
  activeColor?: string; inactiveColor?: string;
}> = ({lyrics, backgroundColor = '#0F172A', activeColor = '#7C3AED', inactiveColor = '#64748B'}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor, padding: 60}}>
      <div style={{maxWidth: 1600, textAlign: 'center'}}>
        {lyrics.map((line, li) => {
          const lineFrame = frame - li * 20;
          const lineFade = spring({frame: lineFrame, fps, config: {damping: 16}});
          const lineDone = line.words.every(w => frame > w.endFrame);
          if (frame < (line.words[0]?.startFrame ?? Infinity) - 10) return null;
          return (
            <div key={li} style={{
              marginBottom: 16, opacity: lineDone ? 0.3 : lineFade,
              transition: 'opacity 0.3s',
            }}>
              {line.words.map((w, wi) => {
                const active = frame >= w.startFrame && frame <= w.endFrame;
                const wordProgress = active
                  ? interpolate(frame, [w.startFrame, w.endFrame], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})
                  : frame > w.endFrame ? 1 : 0;
                return (
                  <span
                    key={wi}
                    style={{
                      fontSize: 48, fontWeight: 700, fontFamily: 'Inter, sans-serif',
                      color: active ? activeColor : wordProgress > 0 && frame > w.endFrame ? '#CBD5E1' : inactiveColor,
                      marginRight: 12, transition: 'color 0.1s',
                      textShadow: active ? `0 0 20px ${activeColor}40` : 'none',
                    }}
                  >
                    {w.text}
                  </span>
                );
              })}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
