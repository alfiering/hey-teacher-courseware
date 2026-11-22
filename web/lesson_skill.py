"""Read teacher-supplied lesson-writing instructions without executing skill files."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path


MAX_SKILL_BYTES = 2 * 1024 * 1024
MAX_INSTRUCTIONS = 16000

# Skills that ship with the workbench; selectable by id without uploading.
BUILTIN_SKILLS = {
    'builtin:creative': ('内置：创意详案（教材原文＋CASE 创意任务）',
                         Path(__file__).resolve().parent / 'skills' / 'creative_lesson_plan.md'),
}


def builtin_skill_text(skill_id: str) -> str:
    if skill_id not in BUILTIN_SKILLS:
        raise ValueError('内置 Skill 不存在，请重新选择')
    return BUILTIN_SKILLS[skill_id][1].read_text(encoding='utf-8').strip()


def _decode_instructions(data: bytes) -> str:
    try:
        return data.decode('utf-8-sig').strip()
    except UnicodeDecodeError as exc:
        raise ValueError('教案 Skill 须为 UTF-8 文本') from exc


def read_lesson_skill(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext not in ('.md', '.txt', '.zip'):
        raise ValueError('教案 Skill 请上传 SKILL.md、文字文件或包含 SKILL.md 的 ZIP')
    if not data or len(data) > MAX_SKILL_BYTES:
        raise ValueError('教案 Skill 文件须非空且不超过 2 MB')
    if ext == '.zip':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > 200 or sum(m.file_size for m in members) > MAX_SKILL_BYTES:
                    raise ValueError('Skill 压缩包文件过多或解压后超过 2 MB')
                for member in members:
                    parts = Path(member.filename).parts
                    if (member.filename.startswith(('/', '\\')) or '\\' in member.filename or
                        '..' in parts or member.flag_bits & 1 or
                        ((member.external_attr >> 16) & 0o170000) == 0o120000):
                        raise ValueError('Skill 压缩包包含不安全路径、符号链接或加密文件')
                candidates = [m for m in members if not m.is_dir() and
                              Path(m.filename).name == 'SKILL.md' and
                              '__MACOSX' not in Path(m.filename).parts]
                if len(candidates) != 1:
                    raise ValueError('Skill 压缩包必须恰好包含一个 SKILL.md')
                skill_file = candidates[0]
                text = _decode_instructions(archive.read(skill_file))
                prefix = skill_file.filename.rsplit('/', 1)[0] + '/' if '/' in skill_file.filename else ''
                references = []
                for member in members:
                    if member.is_dir() or member.filename == skill_file.filename or not member.filename.startswith(prefix):
                        continue
                    relative = member.filename[len(prefix):]
                    if (Path(relative).suffix.lower() in ('.md', '.txt') and
                        not any(part in ('scripts', '__MACOSX') for part in Path(relative).parts)):
                        references.append((relative, member))
                included = set()
                while True:
                    selected = [(relative, member) for relative, member in references
                                if relative not in included and relative in text]
                    if not selected:
                        break
                    for relative, member in selected:
                        if len(included) >= 8:
                            raise ValueError('Skill 引用的文字资料超过 8 份，请精简后上传')
                        included.add(relative)
                        text += f'\n\n【Skill 参考：{relative}】\n' + _decode_instructions(archive.read(member))
                        if len(text) > MAX_INSTRUCTIONS:
                            raise ValueError('教案 Skill 正文须为 20 至 16000 字符的纯文本')
        except zipfile.BadZipFile as exc:
            raise ValueError('Skill 压缩包损坏') from exc
    else:
        text = _decode_instructions(data)
    if '\x00' in text or len(text) < 20 or len(text) > MAX_INSTRUCTIONS:
        raise ValueError('教案 Skill 正文须为 20 至 16000 字符的纯文本')
    return text
