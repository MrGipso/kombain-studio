export type Point = {
  x: number;
  y: number;
  label?: string;
};

export type Zone = {
  label: string;
  points: Point[];
  color?: string;
};

export type MotionGraphicProps = {
  template: string;
  durationSeconds?: number;
  fps?: number;
  title?: string;
  subtitle?: string;
  value?: string;
  label?: string;
  note?: string;
  nodes?: string[];
  chapter?: string;
  accent?: string;
  route?: Point[];
  alternateRoute?: Point[];
  leftLabel?: string;
  rightLabel?: string;
  zones?: Zone[];
};
