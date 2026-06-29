import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface WordCaption {
  text: string;
  startInSeconds: number;
  endInSeconds: number;
  wordIndex?: number;
}

interface CaptionOverlayProps {
  words: WordCaption[];
  wordsPerPage?: number;
  fontSize?: number;
  highlightColor?: string;
  backgroundColor?: string;
}

export const CaptionOverlay: React.FC<CaptionOverlayProps> = ({
  words, wordsPerPage = 6, fontSize = 42,
  highlightColor = '#22D3EE', backgroundColor = 'rgba(15, 23, 42, 0.75)',
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const currentTime = frame / fps;
  const activeIndices = new Set<number>();
  words.forEach((w, i) => {
    if (currentTime >= w.startInSeconds && currentTime < w.endInSeconds) activeIndices.add(i);
  });

  if (activeIndices.size === 0) return null;

  const firstActive = Math.min(...activeIndices);
  const pageStart = Math.floor(firstActive / wordsPerPage) * wordsPerPage;
  const pageWords = words.slice(pageStart, pageStart + wordsPerPage);

  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center', padding: '0 80px 80px', pointerEvents: 'none'}}>
      <div style={{background: backgroundColor, borderRadius: 16, padding: '20px 32px', maxWidth: '80%', display: 'flex', flexWrap: 'wrap', gap: '8px 16px', justifyContent: 'center'}}>
        {pageWords.map((w, i) => {
          const isActive = activeIndices.has(pageStart + i);
          const s = spring({frame: frame - (w.startInSeconds * fps), fps, config: {damping: 20}});
          return (
            <span key={i} style={{
              fontSize, fontFamily: 'Inter, sans-serif', fontWeight: 600,
              color: isActive ? highlightColor : '#94A3B8',
              opacity: isActive ? 1 : Math.min(1, s * 0.6),
              display: 'inline-block',
              textShadow: isActive ? '0 0 20px rgba(34, 211, 238, 0.3)' : 'none',
            }}>
              {w.text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
