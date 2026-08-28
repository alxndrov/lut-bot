"""Логотип клиента из растра в SVG.

Логотипы присылают картинками — цветными, с фоном, с прозрачностью. Для
печати нужен вектор, где всё, кроме фона, залито чёрным: один контур,
который можно масштабировать и резать. Это и делаем: отделяем фон,
остальное считаем силуэтом и обводим его potrace.
"""
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from io import BytesIO

from PIL import Image, ImageChops, ImageOps

logger = logging.getLogger(__name__)

# e2-micro тянет один поток: на большом растре трассировка ощутимо думает,
# а для контура столько точек всё равно не нужно
MAX_SIDE = 1600
TRACE_TIMEOUT = 60
# На сервере 1 ГБ памяти и он же крутит бота: распаковывать гигантский
# растр в оперативку нельзя, иначе положим заодно и бота. JPEG умеет
# распаковываться сразу уменьшенным (draft), поэтому ему потолок выше
MAX_PIXELS = 120_000_000
MAX_PIXELS_RAW = 40_000_000
MAX_BYTES = 20 * 1024 * 1024
MEM_LIMIT = 500 * 1024 * 1024      # потолок памяти отдельного процесса-конвертера
ALPHA_CUT = 128            # прозрачнее этого — фон
PLATE_SHARE = 0.25         # залито больше четверти кадра…
PLATE_FILL = 0.70          # …и почти без просветов — это подложка, а не рисунок
PLATE_MIN_PART = 0.03      # рисунок внутри подложки: не меньше 3% её площади…
PLATE_MAX_PART = 0.45      # …и не больше 45%, иначе это не рисунок, а заливка
PLATE_CONTRAST = 50        # и он должен заметно отличаться по яркости
MIN_INK = 0.001            # меньше 0,1% чёрного — маска пустая, обводить нечего
MAX_INK = 0.98             # почти всё чёрное — фон не отделился

# Так вопрос про логотип сформулирован в брифе; ловим по слову, чтобы
# переформулировка вопроса не отключала конвертацию молча
LOGO_WORDS = ("логотип", "лого", "logo")


def is_logo_question(question: str) -> bool:
    q = (question or "").lower()
    return any(w in q for w in LOGO_WORDS)


# Уже вектор — конвертировать нечего
VECTOR_EXT = {".svg", ".ai", ".eps", ".dxf", ".cdr"}
PDF_DPI = 200


def is_vector(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in VECTOR_EXT


def _pdf_to_image(data: bytes) -> Image.Image | None:
    """Первая страница PDF в растр — логотипы иногда присылают именно так."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        with open(src, "wb") as f:
            f.write(data)
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(PDF_DPI), "-f", "1", "-l", "1",
                            src, os.path.join(tmp, "page")],
                           check=True, capture_output=True, timeout=TRACE_TIMEOUT)
        except Exception as e:
            logger.error(f"logo_svg: pdftoppm не справился: {e}")
            return None
        pages = sorted(p for p in os.listdir(tmp) if p.startswith("page"))
        if not pages:
            return None
        img = Image.open(os.path.join(tmp, pages[0]))
        img.load()          # файл сейчас удалится вместе с каталогом
        return img


def _open(data: bytes) -> Image.Image | None:
    """Растр или PDF — в картинку. Не по расширению: у старых файлов из
    Telegram его просто нет."""
    if len(data) > MAX_BYTES:
        logger.warning(f"logo_svg: файл {len(data) // 1024 // 1024} МБ — слишком большой")
        return None
    if data[:5] == b"%PDF-":
        return _pdf_to_image(data)
    try:
        img = Image.open(BytesIO(data))
    except Exception:
        return None
    w, h = img.size
    limit = MAX_PIXELS if img.format == "JPEG" else MAX_PIXELS_RAW
    if w * h > limit:
        logger.warning(f"logo_svg: картинка {w}x{h} — слишком большая, не распаковываем")
        return None
    if img.format == "JPEG":
        img.draft("RGB", (MAX_SIDE, MAX_SIDE))     # распакуется сразу мельче
    return img


def _otsu(hist: list[int]) -> int:
    """Порог по Оцу: делит гистограмму на фон и не-фон без ручных настроек."""
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_bg = w_bg = 0
    best_t, best_var = 128, -1.0
    for t, h in enumerate(hist):
        w_bg += h
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += t * h
        m_bg = sum_bg / w_bg
        m_fg = (sum_all - sum_bg) / w_fg
        var = w_bg * w_fg * (m_bg - m_fg) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def _background_color(rgb: Image.Image) -> tuple:
    """Цвет фона — то, что по краям: логотип в середине, края почти всегда фон."""
    w, h = rgb.size
    pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
           (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    px = [rgb.getpixel(p) for p in pts]
    return tuple(sorted(c[i] for c in px)[len(px) // 2] for i in range(3))


def _looks_like_plate(mask: Image.Image) -> bool:
    """Не рисунок, а сплошная плашка: залито много и почти без просветов.

    Так выглядит логотип на подложке — белый круг под текстом, тёмный
    квадрат под светлыми буквами. Сам рисунок при этом внутри, и его
    ещё предстоит достать.
    """
    ink = ImageOps.invert(mask.convert("L"))
    box = ink.getbbox()
    if not box:
        return False
    hist = ink.histogram()
    ink_px, total = hist[255], sum(hist)
    box_area = (box[2] - box[0]) * (box[3] - box[1])
    return (ink_px / total > PLATE_SHARE
            and box_area and ink_px / box_area > PLATE_FILL)


def _peel_plate(img: Image.Image, mask: Image.Image) -> Image.Image:
    """Внутри плашки отделяем рисунок: он в меньшинстве, плашка — в большинстве.

    Работает и когда рисунок темнее подложки (красный текст на белом круге),
    и когда светлее (белые линии на чёрном квадрате).
    """
    region = ImageOps.invert(mask.convert("L"))          # белым — то, что внутри
    lum = img.convert("L")
    hist = lum.histogram(region)
    total = sum(hist)
    if not total:
        return mask
    t = _otsu(hist)
    dark = sum(hist[:t + 1])
    light = total - dark
    if not dark or not light:
        return mask
    mean_dark = sum(i * hist[i] for i in range(t + 1)) / dark
    mean_light = sum(i * hist[i] for i in range(t + 1, 256)) / light
    minority = min(dark, light) / total
    # Однотонная заливка (сердце, силуэт) — делить нечего, оставляем как есть
    if not (PLATE_MIN_PART <= minority <= PLATE_MAX_PART):
        return mask
    if mean_light - mean_dark < PLATE_CONTRAST:
        return mask

    ink_is_dark = dark < light
    sel = lum.point([0 if ((i <= t) == ink_is_dark) else 255 for i in range(256)])
    return ImageChops.lighter(sel, ImageOps.invert(region)).convert("1")


def build_mask(img: Image.Image) -> Image.Image:
    """Ч/б маска: фон белый, всё остальное — чёрное (это и пойдёт в контур)."""
    img = ImageOps.exif_transpose(img)
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)

    mask = None
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        alpha = img.getchannel("A")
        if alpha.getextrema()[0] < 250:
            # Прозрачный фон сам себя разметил, порог тут не нужен
            mask = alpha.point(lambda p: 0 if p > ALPHA_CUT else 255).convert("1")

    rgb = img.convert("RGB")
    if mask is None:
        bg = _background_color(rgb)
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg)).convert("L")
        t = _otsu(diff.histogram())
        mask = diff.point(lambda p: 0 if p > t else 255).convert("1")

    if _looks_like_plate(mask):
        mask = _peel_plate(rgb, mask)
    return mask


def _ink_share(mask: Image.Image) -> float:
    black, white = mask.convert("L").histogram()[0], mask.convert("L").histogram()[255]
    total = black + white
    return black / total if total else 0.0


def to_svg_sync(data: bytes, name: str) -> tuple[bytes, str] | None:
    """Растр → SVG чёрным силуэтом. None, если сконвертировать не вышло."""
    img = _open(data)
    if img is None:
        logger.info(f"logo_svg: {name} — не растр и не PDF, конвертировать нечего")
        return None
    try:
        mask = build_mask(img)
    except Exception as e:
        logger.error(f"logo_svg: не разобрал картинку {name}: {e}")
        return None

    ink = _ink_share(mask)
    if not MIN_INK < ink < MAX_INK:
        logger.warning(f"logo_svg: {name} — маска на {ink:.1%} чёрная, фон не отделился")
        return None

    with tempfile.TemporaryDirectory() as tmp:
        pbm, svg = os.path.join(tmp, "in.pbm"), os.path.join(tmp, "out.svg")
        mask.save(pbm)
        try:
            subprocess.run(
                ["potrace", "-s", "--flat", "-t", "4", "-o", svg, pbm],
                check=True, capture_output=True, timeout=TRACE_TIMEOUT)
        except Exception as e:
            logger.error(f"logo_svg: potrace не справился с {name}: {e}")
            return None
        with open(svg, "rb") as f:
            out = f.read()

    return out, os.path.splitext(name or "logo")[0] + ".svg"


async def to_svg(data: bytes, name: str) -> tuple[bytes, str] | None:
    """То же, но отдельным процессом.

    Большой растр распаковывается в сотни мегабайт, а бот живёт на сервере
    с гигабайтом памяти. В своём процессе с жёстким лимитом такой файл в
    худшем случае убьёт только конвертер, а бот продолжит работать.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in"), os.path.join(tmp, "out.svg")
        with open(src, "wb") as f:
            f.write(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "services.logo_svg", src, dst,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=TRACE_TIMEOUT * 2)
        except asyncio.TimeoutError:
            logger.error(f"logo_svg: {name} — конвертер не уложился во время")
            return None
        except Exception as e:
            logger.error(f"logo_svg: не запустился конвертер для {name}: {e}")
            return None
        if proc.returncode != 0 or not os.path.exists(dst):
            msg = (err or b"").decode(errors="replace").strip()[-300:]
            logger.info(f"logo_svg: {name} не сконвертирован ({proc.returncode}) {msg}")
            return None
        with open(dst, "rb") as f:
            svg = f.read()
    return svg, os.path.splitext(name or "logo")[0] + ".svg"


def _worker(src: str, dst: str) -> int:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT, MEM_LIMIT))
    with open(src, "rb") as f:
        data = f.read()
    result = to_svg_sync(data, os.path.basename(src))
    if not result:
        return 1
    with open(dst, "wb") as f:
        f.write(result[0])
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(_worker(sys.argv[1], sys.argv[2]))
