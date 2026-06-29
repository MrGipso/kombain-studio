import React from 'react';
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
  Audio,
} from 'remotion';
import {getTemplate} from './templateRegistry';

interface Cut {
  id: string;
  type: 'image' | 'video' | 'template' | 'text';
  source?: string;
  template?: string;
  templateProps?: Record<string, unknown>;
  text?: string;
  subtitle?: string;
  in_seconds: number;
  out_seconds: number;
  [key: string]: unknown;
}

interface AudioTrack {
  src: string;
  volume?: number;
  offsetSeconds?: number;
  [key: string]: unknown;
}

export interface VideoComposerProps {
  cuts: Cut[];
  audio?: AudioTrack[];
  fps?: number;
  transitionFrames?: number;
  [key: string]: unknown;
}

const resolveAsset = (src: string): string => {
  if (src.startsWith('http://') || src.startsWith('https://') || src.startsWith('data:')) {
    return src;
  }
  const clean = src.replace(/^file:\/\/\/?/, '');
  if (clean.startsWith('/') || /^[A-Za-z]:[\\/]/.test(clean)) {
    return `file:///${clean.replace(/\\/g, '/')}`;
  }
  return src;
};

const Vignette: React.FC = () => (
  <AbsoluteFill
    style={{
      background:
        'radial-gradient(ellipse at center, transparent 50%, rgba(0,0,0,0.6) 100%)',
      pointerEvents: 'none',
    }}
  />
);

const ImageCut: React.FC<{src: string}> = ({src}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();

  const fadeIn = spring({frame, fps, config: {damping: 18, stiffness: 80}});
  const fadeOutStart = durationInFrames - 8;
  const fadeOut = interpolate(frame, [fadeOutStart, durationInFrames], [1, 0.3], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const progress = interpolate(frame, [0, durationInFrames], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const scale = 1 + progress * 0.18;
  const translateY = interpolate(progress, [0, 1], [0, -20]);

  return (
    <AbsoluteFill style={{overflow: 'hidden', background: '#0F172A'}}>
      <Img
        src={resolveAsset(src)}
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          opacity: fadeIn * fadeOut,
          transform: `scale(${scale}) translateY(${translateY}px)`,
        }}
      />
      <Vignette />
    </AbsoluteFill>
  );
};

const VideoCut: React.FC<{src: string}> = ({src}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();

  const fadeIn = spring({frame, fps, config: {damping: 20}});
  const fadeOutStart = durationInFrames - 8;
  const fadeOut = interpolate(frame, [fadeOutStart, durationInFrames], [1, 0.3], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  return (
    <AbsoluteFill style={{background: '#0F172A'}}>
      <OffthreadVideo
        src={resolveAsset(src)}
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          opacity: fadeIn * fadeOut,
        }}
        muted
      />
      <Vignette />
    </AbsoluteFill>
  );
};

const TextCut: React.FC<{text: string; subtitle?: string}> = ({text, subtitle}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const progress = spring({frame, fps, config: {damping: 16, stiffness: 70}});

  return (
    <AbsoluteFill
      style={{
        background: 'linear-gradient(135deg, #0F172A, #1E293B)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 80,
      }}
    >
      <div
        style={{
          opacity: progress,
          transform: `translateY(${(1 - progress) * 40}px)`,
          textAlign: 'center',
        }}
      >
        <div
          style={{
            fontSize: 64,
            fontWeight: 700,
            color: '#F8FAFC',
            fontFamily: 'system-ui, sans-serif',
            lineHeight: 1.3,
            marginBottom: subtitle ? 24 : 0,
          }}
        >
          {text}
        </div>
        {subtitle && (
          <div
            style={{
              fontSize: 32,
              fontWeight: 400,
              color: '#94A3B8',
              fontFamily: 'system-ui, sans-serif',
            }}
          >
            {subtitle}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};

const TemplateCut: React.FC<{template: string; props: Record<string, unknown>}> = ({
  template,
  props,
}) => {
  const Component = getTemplate(template);
  if (!Component) {
    return (
      <AbsoluteFill
        style={{
          background: '#0b1020',
          color: '#ef4444',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 32,
          fontFamily: 'monospace',
        }}
      >
        Unknown template: {template}
      </AbsoluteFill>
    );
  }
  return (
    <AbsoluteFill>
      <Component {...props} />
    </AbsoluteFill>
  );
};

export const VideoComposer: React.FC<VideoComposerProps> = ({
  cuts,
  audio,
  transitionFrames = 0,
}) => {
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill style={{background: '#0F172A'}}>
      {cuts.map((cut) => {
        const from = Math.round(cut.in_seconds * fps);
        const duration = Math.round((cut.out_seconds - cut.in_seconds) * fps);

        let children: React.ReactNode = null;
        if (cut.type === 'image' && cut.source) {
          children = <ImageCut src={cut.source} />;
        } else if (cut.type === 'video' && cut.source) {
          children = <VideoCut src={cut.source} />;
        } else if (cut.type === 'template' && cut.template) {
          children = <TemplateCut template={cut.template} props={cut.templateProps ?? {}} />;
        } else if (cut.type === 'text') {
          children = <TextCut text={cut.text ?? ''} subtitle={cut.subtitle} />;
        } else if (cut.source) {
          const ext = cut.source.toLowerCase();
          if (ext.endsWith('.mp4') || ext.endsWith('.webm') || ext.endsWith('.mov')) {
            children = <VideoCut src={cut.source} />;
          } else {
            children = <ImageCut src={cut.source} />;
          }
        } else {
          children = <TextCut text={cut.text ?? cut.id} subtitle={cut.subtitle} />;
        }

        return (
          <Sequence key={cut.id} from={from} durationInFrames={duration}>
            {children}
          </Sequence>
        );
      })}

      {audio?.map((track, i) => (
        <Audio
          key={`audio-${i}`}
          src={resolveAsset(track.src)}
          startFrom={Math.round((track.offsetSeconds ?? 0) * fps)}
          volume={track.volume ?? 1}
        />
      ))}
    </AbsoluteFill>
  );
};
