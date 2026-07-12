from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class VideoStyle:
    id: str
    name: str
    description: str
    scene_brief: str
    base_prompt: str
    character_rules: str
    shot_planning_rules: list[str]
    visual_translation_rules: list[str]
    negative_prompt: str
    example_bad: str = ""
    example_good: str = ""
    # Short ENGLISH animation cue appended to every i2v video prompt
    # (see compose_video_prompt). Style-specific so a photoreal style does not
    # inherit stop-motion puppet movement.
    motion_cue: str = (
        "subtle handmade stop-motion motion, gentle puppet-like movement, stable knit textures"
    )

    def to_public_dict(self) -> dict:
        return asdict(self)


KNITTED = VideoStyle(
    id="knitted",
    name="Вязанный",
    description=(
        "Вязаная stop-motion диорама: amigurumi-персонажи, видимые петли, "
        "шерстяные волокна, миниатюрные handmade-декорации и мягкий макро-свет."
    ),
    scene_brief=(
        "Показывай РЕАЛЬНОЕ узнаваемое место, объект или сцену из текста — но целиком "
        "связанное из пряжи: вязаные здания, техника, предметы и окружение, видимые петли "
        "и текстура вязания, а люди — amigurumi-куклы. НЕ превращай тему в безликую "
        "абстрактную миниатюру и не уходи в сплошное макро — зритель должен УЗНАВАТЬ "
        "конкретную локацию (улица города, серверная, корабль, биржа, кабинет, побережье и т.п.) "
        "и понимать действие даже без озвучки. Реальные ориентиры и объекты должны быть "
        "распознаваемы, просто в вязаном исполнении. Без символов и метафор — конкретная сцена."
    ),
    base_prompt=(
        # MATERIAL/TECHNIQUE ONLY — no subject nouns here. This string is appended
        # to EVERY image, so any object named here (car, building, character...) is
        # forced into every frame. Subjects come solely from the per-shot body.
        "entirely handmade from crochet and knitted yarn, visible stitches, soft wool "
        "fibers, felted textile surfaces, tactile hand-knit materials, miniature "
        "stop-motion diorama look, the real place kept recognizable by its own landmarks, "
        "soft cinematic lighting, mostly wide and medium framing, 16:9 frame"
    ),
    character_rules=(
        "If a recurring character is present, keep the same amigurumi proportions, yarn colors, "
        "face stitching, outfit colors, and silhouette in every shot."
    ),
    shot_planning_rules=[
        "Use 1-5 concrete shots per semantic scene.",
        "Favor establishing and medium shots that clearly show the REAL, recognizable place; use close-ups sparingly, NOT as the default. Avoid defaulting to extreme macro of textile.",
        "Each shot depicts a recognizable real location or object from the script — just knitted.",
        "Keep each shot simple: one clear action and 1-3 main objects, in an identifiable setting.",
        "Represent conflict as tangled yarn, frayed stitches, dimmer light, or uneven fabric — but inside a recognizable real scene.",
        "Represent progress as neat stitching, brighter warm light — inside a recognizable real scene.",
    ],
    visual_translation_rules=[
        "Humans become amigurumi dolls doing ONE clear action in a real, recognizable setting.",
        "Animals become crocheted plush figures.",
        "A real place stays the SAME place, still recognizable by its own landmarks, only rendered in yarn and stitches. Render ONLY the objects that this specific scene actually contains — do not add cars, people, or props that the script did not mention.",
        "Objects and machines keep their real, recognizable form, made of yarn — do not abstract them into generic textile blobs.",
        "Abstract ideas: pick ONE concrete, recognizable scene that shows the idea through action with named objects.",
        "Emotions: show via a clear pose and stitched facial expression in a concrete situation.",
    ],
    negative_prompt=(
        "Avoid photorealistic human skin, smooth plastic, glossy CGI, flat digital cartoon, "
        "realistic face, metallic surfaces, latex, readable text, logos, extra limbs, distorted hands."
    ),
    example_bad="Хрупкость интернета, показанная распускающейся нитью (абстракция — так НЕ делай).",
    example_good=(
        "Вязаная рука осторожно держит одну тонкую светящуюся нить над миниатюрной "
        "океанской диорамой, кончик нити начинает распускаться."
    ),
)


ROBO_PRESENT = VideoStyle(
    id="robo_present",
    name="Роботы среди нас",
    description=(
        "Мягкий полуреалистичный 3D: обычный сегодняшний мир — серверные, офисы, "
        "улицы, настоящее железо — а действуют в нём обаятельные компактные роботы. "
        "Без футуризма: мир наш, роботы просто милые."
    ),
    scene_brief=(
        "Показывай РЕАЛЬНОЕ узнаваемое место, объект или сцену из текста как мягкий "
        "полуреалистичный 3D-рендер: серверная с рядами стоек, датацентр, обычный офис, "
        "рабочий стол с компьютером, склад, магазин электроники, городская улица. "
        "Всё оборудование и обстановка — строго СЕГОДНЯШНИЕ и узнаваемые: видеокарты, "
        "мониторы, кабели, вентиляторы, коробки. Никакого футуризма, неона, голограмм "
        "и «мира будущего». Главные действующие лица — ОБАЯТЕЛЬНЫЕ КОМПАКТНЫЕ РОБОТЫ: "
        "округлый корпус, простые выразительные глаза-дисплей, короткие руки, колёсики "
        "или гусеницы — как правдоподобные сервисные роботы наших дней, милые, но не "
        "андроиды из фантастики. Роботы делают понятные бытовые действия с настоящей "
        "техникой: вставляют видеокарту, тянут кабель, везут коробку, смотрят на монитор. "
        "Зритель должен узнавать конкретную локацию и понимать действие без озвучки. "
        "Без символов и метафор — конкретная сцена."
    ),
    base_prompt=(
        # MATERIAL/TECHNIQUE ONLY — no subject nouns here (see KNITTED note).
        # The robot characters come from the per-shot body via the translation
        # rules; naming them here would force a robot into every single frame.
        "as a soft semi-realistic 3D render, gently rounded shapes, matte "
        "physically-based materials, warm studio lighting, shallow depth of field, "
        "ordinary present-day setting, the real place kept recognizable by its own "
        "landmarks, mostly wide and medium framing, 16:9 frame"
    ),
    character_rules=(
        "The recurring robot character must look IDENTICAL in every shot: same compact "
        "rounded body, same shell colors, same simple display-screen eyes, same stubby "
        "arms and wheels/treads, same proportions. Expressions change only via the eye "
        "display and posture."
    ),
    shot_planning_rules=[
        "Use 1-5 concrete shots per semantic scene.",
        "Favor establishing and medium shots that clearly show the REAL, recognizable place; use close-ups sparingly, NOT as the default.",
        "Each shot depicts a recognizable real present-day location or object from the script; the actor in the scene is the cute robot.",
        "Keep each shot simple: one clear action and 1-3 main objects, in an identifiable setting.",
        "The world is strictly present-day: ordinary modern hardware, offices, streets. NO futuristic tech, NO holograms, NO neon sci-fi. The only whimsical element is the cute robot itself.",
        "Represent conflict as clutter, tangled cables, warning lights, a drooping robot posture with dim eye display — inside a recognizable real scene.",
        "Represent progress as tidy order, warm brighter light, green indicator lights, an upright robot with bright cheerful eye display — inside a recognizable real scene.",
    ],
    visual_translation_rules=[
        "Humans become cute compact robots doing ONE clear action in a real, recognizable present-day setting. A robot is the protagonist of nearly every shot.",
        "The robot is charming but plausible: rounded friendly body, display-screen eyes, stubby arms, wheels or treads — like a believable modern service robot. NEVER a sci-fi humanoid android, never weapons or armor.",
        "Computers, GPUs, servers and machines keep their real, recognizable present-day form — do not stylize them into abstract shapes; only the robot characters are cute.",
        "A real place stays the SAME place, recognizable by its own landmarks. Render ONLY the objects that this specific scene actually contains.",
        "AI and neural networks are shown through their PHYSICAL carriers: server racks, graphics cards, monitors with code or a chat window, cables, cooling fans — with the robot interacting with them.",
        "Emotions: show via the robot's eye display (simple pixel eyes) and body posture in a concrete situation.",
        "Abstract ideas: pick ONE concrete, recognizable scene that shows the idea through the robot's action with named objects.",
    ],
    negative_prompt=(
        "Avoid futuristic sci-fi cityscapes, cyberpunk neon, holograms, glowing circuits, "
        "humanoid androids with human faces, weapons, photorealistic human skin, "
        "readable text, logos, extra limbs, distorted hands."
    ),
    example_bad=(
        "Искусственный интеллект как светящийся цифровой мозг из частиц над городом "
        "будущего (абстракция и футуризм — так НЕ делай)."
    ),
    example_good=(
        "Обычная серверная: округлый робот с глазами-дисплеем аккуратно вставляет "
        "видеокарту в открытую стойку, рядом тележка с коробками, мигают зелёные "
        "индикаторы."
    ),
    motion_cue=(
        "smooth gentle motion, small expressive robot movements, slow camera push-in "
        "or drift, stable clean matte surfaces"
    ),
)


DOCUMENTARY = VideoStyle(
    id="documentary",
    name="Документальный",
    description=(
        "Фотореалистичный документальный кинематограф: реальные локации, люди, "
        "объекты и явления, снятые как для научпоп-фильма. Тёплый киносвет и живые "
        "текстуры. То, что нельзя снять камерой (молекулы, космос, процессы), "
        "показывается как чистая научная 3D-визуализация."
    ),
    scene_brief=(
        "Показывай РЕАЛЬНУЮ узнаваемую сцену из текста так, будто её сняли для "
        "документального фильма: настоящая локация, живые люди, реальные предметы и "
        "явления, естественный свет. Зритель должен УЗНАВАТЬ конкретное место, время "
        "и действие даже без озвучки (улица города, лес, лаборатория, кухня, цех, "
        "побережье, кабинет, историческая сцена и т.п.). "
        "Если тему НЕЛЬЗЯ снять камерой (молекула, атом, вирус, орбита планеты, "
        "невидимый процесс) — покажи её как ЧИСТУЮ, наглядную научную 3D-визуализацию: "
        "конкретную модель самого объекта на нейтральном или тёмном фоне, без надписей. "
        "Это не метафора, а буквальное изображение объекта. "
        "Без символов, аллегорий и абстрактных «облаков смысла» — только конкретная "
        "сцена или конкретная модель."
    ),
    base_prompt=(
        # MATERIAL/TECHNIQUE ONLY — no subject nouns here (see KNITTED note). This
        # string is appended to EVERY image, so naming any object would force it
        # into every frame. Subjects come solely from the per-shot body.
        "as photorealistic cinematic documentary footage, shot on a professional "
        "cinema camera, natural motivated lighting, lifelike materials and textures, "
        "true-to-life color, cinematic depth of field, the real place and time kept "
        "recognizable by their own details, mostly wide and medium framing, 16:9 frame"
    ),
    character_rules=(
        "There is usually NO recurring hero. Show people realistically as part of the "
        "scene — hands at work, silhouettes, seen over-the-shoulder or from behind, "
        "small figures in a wide shot — to avoid an uncanny repeated face. If a "
        "specific recurring subject IS described, keep the same appearance, age, "
        "clothing and build in every shot."
    ),
    shot_planning_rules=[
        "Use 1-5 concrete shots per semantic scene.",
        "Favor establishing and medium shots that clearly show the REAL, recognizable place, time and action; use close-ups and macro purposefully, NOT as the default.",
        "Each shot is a literal documentary frame: a real location, object, person or natural phenomenon from the script.",
        "For a subject that genuinely cannot be filmed (molecule, atom, cell, orbit, invisible process), use ONE clean scientific 3D-visualization shot of that exact object on a neutral or dark background — a concrete model, not a metaphor.",
        "Keep each shot simple: one clear action or subject and 1-3 main objects, in an identifiable setting.",
        "Represent conflict or danger through real cues: storm, decay, damage, tension, dim natural light — inside a recognizable real scene.",
        "Represent progress or resolution through real cues: clear skies, order, growth, warm natural light — inside a recognizable real scene.",
    ],
    visual_translation_rules=[
        "Humans become REAL people doing ONE clear action in a real, recognizable setting — or anonymous b-roll (hands, silhouette, back, crowd) when no specific person is meant.",
        "Animals become real animals in their real habitat.",
        "A real place stays the SAME real place, recognizable by its own details; render ONLY the objects the scene actually contains — do not add props the script did not mention.",
        "Objects, machines and instruments keep their real, recognizable form and real materials.",
        "Unfilmable science (molecules, atoms, cells, forces, orbits, invisible processes) becomes a clean, literal 3D scientific visualization of that exact object on a neutral or dark background — clear and accurate, never an abstract cloud of particles.",
        "Data and numbers appear on real physical carriers present in the scene: a screen, a printed chart, a lab display, a notebook.",
        "History becomes a period-accurate real scene (clothing, tools, architecture of that era).",
        "Emotions: show through real human body language and the concrete situation, not symbols.",
    ],
    negative_prompt=(
        "Avoid cartoon, anime, illustration, painterly look, stylized 3D, video-game "
        "render, plastic or toy surfaces, oversaturated fantasy colors, surreal "
        "abstraction, readable text, captions, logos, watermark, extra limbs, "
        "distorted hands, deformed or waxy faces."
    ),
    example_bad=(
        "Эволюция как светящееся дерево из частиц, растущее в абстрактной пустоте "
        "(абстракция и метафора — так НЕ делай)."
    ),
    example_good=(
        "Палеонтолог мягкой кистью счищает песок с частично раскопанной кости "
        "динозавра в раскопе под тёплым дневным светом, рядом разложены инструменты."
    ),
    motion_cue=(
        "subtle realistic camera movement, slow dolly or gentle push-in, natural "
        "handheld micro-motion, lifelike ambient motion, no morphing, stable "
        "photoreal detail"
    ),
)


AI_WORLD = VideoStyle(
    id="ai_world",
    name="Мир ИИ",
    description=(
        "Тема ИИ и LLM без роботов: реальный мир вычислений — серверные, GPU, "
        "датацентры, мониторы с кодом и чатом — плюс чистые научные 3D-визуализации "
        "нейросетей и потоков данных. Документальный фото-реализм: снято как на настоящую "
        "камеру, естественный разнообразный свет, живые несовершенства — БЕЗ глянцевого "
        "3D-грейда и без единой сине-бирюзовой заливки."
    ),
    scene_brief=(
        "Показывай тему ИИ/LLM через РЕАЛЬНОЕ железо и явления, а НЕ через роботов и не "
        "через персонажа. Узнаваемые современные сцены: серверная с рядами стоек, датацентр, "
        "открытая видеокарта, материнская плата, кабели и охлаждение, монитор с бегущим кодом "
        "или окном чата, рабочее место инженера. Всё оборудование — СЕГОДНЯШНЕЕ и узнаваемое, "
        "без футуристического неона и «мира будущего». "
        "Если тема про локальные модели, llama.cpp, GGUF, квантизацию, CPU/GPU inference, "
        "tokens/sec, установку, сборку или запуск из командной строки — заземляй кадры в "
        "локальной инженерной реальности: обычный ноутбук или desktop workstation, терминал "
        "с размытыми строками, папка локальных model files, график benchmark, CPU/RAM/GPU, "
        "домашняя лаборатория, открытый репозиторий, сборка проекта, тепловой шум кулеров. "
        "В таких местах предпочитай локальный компьютер и рабочий стол инженера, а не generic "
        "датацентр. "
        "Если понятие нельзя снять (нейросеть, эмбеддинги, токены, обучение, внимание) — есть два "
        "инструмента. Первый и предпочтительный: ПРАКТИЧЕСКАЯ АНАЛОГИЯ — предметная съёмка "
        "бытовых объектов, физически демонстрирующих идею (квантизация — одна и та же фотография, "
        "напечатанная в убывающем качестве, разложенная на столе; токены — лист текста, нарезанный "
        "ножницами на кусочки; контекстное окно — рабочий стол, заваленный бумагами, с края "
        "падают старые листы). Описывай такой кадр как буквальную физическую сцену с предметами, "
        "без слов «символизирует/represents» — связь с понятием даёт голос диктора. Второй, реже: "
        "ЧИСТАЯ наглядная 3D-визуализация на тёмном фоне — аккуратный граф узлов и связей, "
        "поток данных, лента токенов, слои сети; буквальная схема объекта, а не метафора. "
        "Не делай generic AI wallpaper: каждый кадр должен показывать конкретный инженерный "
        "объект, действие, интерфейс, benchmark, компонент железа или локальную setup-сцену, "
        "связанную с текстом. "
        "Люди — только безликий b-roll (руки на клавиатуре, силуэт у стойки, со спины). "
        "Без роботов, андроидов, талисманов и «светящегося мозга из частиц»."
    ),
    base_prompt=(
        # MATERIAL/TECHNIQUE ONLY — no subject nouns here (see KNITTED note).
        "photographed on a real camera, documentary realism, natural available light with "
        "varied colour temperature from shot to shot, true-to-life colour, subtle sensor grain "
        "and real lens imperfections, present-day computing hardware kept recognizable, "
        "unpolished believable environments, candid framing, a mix of wide, medium and "
        "occasional close shots, 16:9 frame. Avoid an over-polished advertising sheen on real "
        "hardware and avoid a uniform blue-teal colour grade (clean 3D diagram shots are the "
        "only exception and stay crisp)."
    ),
    character_rules=(
        "NO recurring character and NO robots. People appear only as anonymous b-roll — hands on "
        "a keyboard, a silhouette at a workstation, a figure seen from behind — never a repeated "
        "face and never a machine mascot."
    ),
    shot_planning_rules=[
        "Use 1-5 concrete shots per semantic scene.",
        "Favor establishing and medium shots of REAL, recognizable present-day AI hardware and workspaces; use close-ups (a GPU, a cable, a screen) purposefully, not as the default.",
        "VARIETY IS MANDATORY. Consecutive shots must NOT look alike. Deliberately vary the framing (wide / establishing / medium / over-the-shoulder / detail), the angle (eye-level, low, high, top-down), the setting, and the time-of-day/light. Never open more than two shots in a row with the same kind of subject.",
        "Do NOT lean on a glowing monitor as the go-to image. Rotate across a WIDE real vocabulary: an engineer's desk seen wide, a whiteboard sketch, printed notes and a coffee mug, a laptop on a lap, a server room walked through, an open PC case on a bench, a hand swapping a RAM stick, a laptop outdoors, two people reviewing a screen from behind, a bookshelf of hardware, packaging and cables on a floor, a cooling loop, a power supply, a thermal camera view. Pick what actually fits the line, but keep the set diverse.",
        "When the scene mentions local models, llama.cpp, GGUF, quantization, CPU/GPU inference, installation, compiling, command-line use, benchmarks, tokens/sec, RAM or VRAM, prefer local-engineering visuals: a laptop terminal, desktop workstation, home-lab desk, motherboard, RAM sticks, CPU heat sink, GPU cooler, local model files, benchmark graph, or open-source repository workflow.",
        "Use generic data centers only for scale/cloud/infrastructure moments. For local inference and llama.cpp moments, show the ordinary machine on the desk.",
        "For an unfilmable AI concept (a neural network, embeddings, tokens, training, attention), use ONE clean 3D visualization shot of that exact thing on a dark background — a concrete diagram, not a metaphor.",
        "Keep each shot simple: one clear subject and 1-3 main objects, in an identifiable setting.",
        "Avoid generic beautiful AI wallpaper. Every shot must answer: what specific technical thing is happening here?",
        "On screens, terminals and charts, show blurred command-like lines, clean UI blocks, graphs or code-shaped columns; do NOT ask for readable exact text or fake words.",
        "Diagrams are the EXCEPTION, not the default: at most ONE clean 3D-visualization shot per 4-5 grounded engineering shots, and only for a concept that truly cannot be filmed (a neural net, embeddings, attention). Everything else must be grounded in real hardware and workspaces: hands at a keyboard, a local workstation, a real component, a benchmark screen, a cable/thermal detail.",
        "PRACTICAL ANALOGY shots are the most engaging tool for explaining a concept — prefer them over a diagram when the narrator explains an abstraction. Stage everyday objects as a tabletop demonstration, shot like real product photography: the same photo printed at decreasing quality laid out in a row (quantization), a printed page cut into small strips with scissors (tokens), a desk overflowing with papers where the oldest sheets fall off the edge (context window), water poured through funnels of different widths (throughput). Describe ONLY the literal physical scene and objects — never write 'represents' or 'symbolizes' in the prompt; the voice-over makes the connection. Use at most ONE such shot per scene, only where the narration explains a concept.",
        "Represent conflict/failure through real cues: warning lights, thermal throttling, tangled cables, error text on a screen, a dim room.",
        "Represent progress/scale through real cues: rows of green indicators, tidy racks, a clean bright workstation, smooth data on screens.",
    ],
    visual_translation_rules=[
        "AI, models, LLMs and neural networks become their PHYSICAL carriers: server racks, graphics cards, monitors showing code or a chat window, cables, cooling fans — OR a clean labeled 3D neural-net / data-flow visualization on a dark background. NEVER a robot, NEVER a glowing particle brain.",
        "llama.cpp, local LLMs, GGUF files, quantization, CPU inference, GPU offload, context length, tokens/sec and benchmarks become concrete local-computing scenes: a terminal on an ordinary laptop, a desktop workstation with visible CPU/RAM/GPU, local model files in a folder, a simple benchmark graph, a compiler/build progress window, or a developer's hands running commands.",
        "Prefer local workstation / home-lab / developer desk visuals over server farms whenever the script discusses running a model locally, installing tools, compiling, quantizing, benchmarking, or choosing hardware.",
        "Data, tokens, embeddings, weights become clean 3D graphs: nodes and edges, flowing streams, a ribbon of tokens, network layers — accurate and legible, not an abstract cloud.",
        "Humans become anonymous b-roll (hands typing, silhouette, back) in a real tech setting — no recurring face.",
        "A real place stays the SAME place, recognizable by its own details; render ONLY the objects the scene actually contains.",
        "Chat, code, terminals, dashboards and benchmark screens appear as real screens with plausible UNREADABLE content: blurred command-like lines, blocky UI rows, soft graphs, cropped terminal panes. Never request readable fake words, exact commands, logos or legible gibberish.",
        "Open-source/community ideas become grounded workflow images: a repository-like web page with unreadable rows, issue/commit-shaped interface blocks, a developer desk, or shared code review on monitors — never a logo reproduction.",
        "A concept the narrator EXPLAINS becomes a tabletop practical-analogy demonstration with everyday objects (printed photos, cut paper, stacked books, funnels and water, weights on a scale) shot as clean product photography with natural light — described as a literal scene, no 'represents'/'symbolizes' wording.",
        "Abstract ideas: pick ONE concrete real scene (engineer at a rack, code on a monitor), ONE tabletop practical-analogy demonstration, or ONE clean 3D diagram that shows the idea literally.",
    ],
    negative_prompt=(
        "Avoid robots, androids, humanoid machines, cute mascots, glowing particle brains, "
        "human-brain imagery, futuristic sci-fi cityscapes, cyberpunk neon overload, holograms, "
        "fantasy, cartoon, anime, readable text, fake words, gibberish UI text, logos, watermark, "
        "generic AI wallpaper, decorative data particles without a concrete engineering object, "
        "extra limbs, distorted hands. "
        "Also avoid: glossy 3D product-render sheen on real hardware, plastic CGI look, "
        "uniform blue-teal or teal-orange colour grade, over-clean studio lighting, "
        "and repetitive framing — do NOT default every shot to a close-up of a glowing monitor."
    ),
    example_bad=(
        "Искусственный интеллект как светящийся мозг из частиц, милый робот у компьютера или "
        "красивый абстрактный поток данных без конкретного железа/действия (так НЕ делай)."
    ),
    example_good=(
        "Обычный рабочий стол инженера: desktop workstation с открытой боковой панелью, видны "
        "GPU, RAM и кулер CPU; на мониторе размытый терминал и простой график tokens/sec, рядом "
        "папка локальных model files, обычный свет комнаты, снято как на настоящую камеру."
    ),
    motion_cue=(
        "handheld-style micro-motion or a slow natural camera drift, subtle blinking indicators "
        "and streaming data on screens, real-world ambient movement, no morphing, true-to-life "
        "detail rather than a polished render"
    ),
)


VIDEO_STYLES: dict[str, VideoStyle] = {
    KNITTED.id: KNITTED,
    ROBO_PRESENT.id: ROBO_PRESENT,
    DOCUMENTARY.id: DOCUMENTARY,
    AI_WORLD.id: AI_WORLD,
}


DEFAULT_VIDEO_STYLE = "knitted"


def get_video_style(style_id: str | None) -> VideoStyle:
    return VIDEO_STYLES.get((style_id or "").strip().lower(), VIDEO_STYLES[DEFAULT_VIDEO_STYLE])


def list_video_styles() -> list[dict]:
    return [style.to_public_dict() for style in VIDEO_STYLES.values()]


def style_director_brief(style_id: str | None, custom_notes: str = "") -> str:
    style = get_video_style(style_id)
    lines = [
        f"ВЫБРАННЫЙ СТИЛЬ: {style.name}",
        style.scene_brief,
        "ПРАВИЛО КОНКРЕТНОСТИ (важнее стиля):",
        "- Каждый кадр — буквальная физическая сцена: конкретный субъект + одно понятное действие + место + 1-3 предмета.",
        "- Запрещены метафоры и слова: символизирует, олицетворяет, представляет, концепция, abstract, represents, symbolizes, metaphor.",
        "- Зритель должен понять кадр без дикторского текста. Если не можешь представить кадр буквально — выбери более простую конкретную сцену.",
        "Правила планирования кадров:",
        *[f"- {rule}" for rule in style.shot_planning_rules],
        "Правила визуального перевода:",
        *[f"- {rule}" for rule in style.visual_translation_rules],
    ]
    if style.example_bad or style.example_good:
        lines.append("Пример конкретности:")
        if style.example_bad:
            lines.append(f"- ПЛОХО (абстрактно): {style.example_bad}")
        if style.example_good:
            lines.append(f"- ХОРОШО (конкретно): {style.example_good}")
    if custom_notes.strip():
        lines.extend(["Дополнительные заметки пользователя:", custom_notes.strip()])
    return "\n".join(lines)


def compose_image_prompt(
    style_id: str | None,
    shot_prompt: str,
    character_description: str = "",
    custom_notes: str = "",
) -> str:
    style = get_video_style(style_id)
    body = " ".join((shot_prompt or "").split()).strip() or "A simple handmade miniature scene."
    # POSITIVE prompt only, SUBJECT FIRST. Krea weights early tokens most, so the
    # concrete scene leads (keeps the real place recognizable) and the material/
    # technique is a trailing finish. base_prompt is MATERIAL-ONLY, so it never
    # injects objects. No negative text here — Krea Turbo zeroes the negative via
    # ConditioningZeroOut; negatives in a positive prompt only pull the model
    # toward what it should avoid. Prompt-writing rules live in INSTRUCTIONS.
    parts = [
        body,
        f"Rendered {style.base_prompt}.",
    ]
    if character_description.strip():
        parts.append(f"Character consistency: {character_description.strip()}. {style.character_rules}")
    if custom_notes.strip():
        parts.append(f"Additional style notes: {custom_notes.strip()}")
    return "\n\n".join(parts)


def compose_video_prompt(style_id: str | None, motion_prompt: str, shot_description: str = "") -> str:
    # POSITIVE video prompt only, ENGLISH only: the per-shot motion + a short
    # positive style cue. We do NOT append the shot description here — it is Russian
    # (written for the editor) and would inject mixed language into an English i2v
    # prompt; the image already defines the look for image-to-video. We also don't
    # append "avoid morphing/melting" — negatives in a positive prompt only pull the
    # model toward those failures. `shot_description` is kept for signature
    # compatibility but intentionally unused.
    style = get_video_style(style_id)
    motion = " ".join((motion_prompt or "").split()).strip()
    if not motion:
        motion = "The camera slowly pushes in and holds steady, with only small ambient motion; the subject stays still."
    return (
        f"{motion} "
        f"Animation style: {style.motion_cue}."
    )
