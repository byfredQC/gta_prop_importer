# GTA Prop Importer - tape le nom d'un prop GTA V, il arrive dans Blender avec ses textures.
# Necessite : Sollumz (addon Blender), CodeWalker (pour CodeWalker.Core.dll), GTA V installe, Windows.

import bpy
import os
import sys
import json
import queue
import shutil
import hashlib
import threading
import subprocess
from pathlib import Path
from bpy.props import StringProperty, BoolProperty, IntProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

ADDON_DIR = Path(__file__).parent
SOURCE_CS = ADDON_DIR / "PropGrabber.cs"


# ---------------------------------------------------------------- utilitaires

def prefs():
    return bpy.context.preferences.addons[__package__].preferences


def is_en() -> bool:
    try:
        return prefs().language == "EN"
    except Exception:
        return False


def L(fr: str, en: str) -> str:
    """Texte dans la langue choisie (preferences de l'addon) / text in the chosen language"""
    return en if is_en() else fr


# Messages envoyes par PropGrabber (en francais) -> anglais
TOOL_MSGS = (
    ("Chargement des cles du jeu...", "Loading game keys..."),
    ("Scan des archives RPF (1 a 2 minutes la premiere fois)...", "Scanning RPF archives (1-2 minutes the first time)..."),
    ("Modele ", "Model "),
    ("Prop introuvable : ", "Prop not found: "),
    ("Ymap introuvable : ", "Ymap not found: "),
    ("Impossible de lire ", "Cannot read "),
    ("Dossier GTA V introuvable : ", "GTA V folder not found: "),
    ("Commande inconnue : ", "Unknown command: "),
    ("Nom vide", "Empty name"),
    ("Usage : PropGrabber.exe <dossier CodeWalker> <dossier GTA V>", "Usage: PropGrabber.exe <CodeWalker folder> <GTA V folder>"),
)


def tool_msg(text: str) -> str:
    if not text or not is_en():
        return text
    for fr, en in TOOL_MSGS:
        if text.startswith(fr):
            return en + text[len(fr):]
    return text


def user_dir() -> Path:
    try:
        d = Path(bpy.utils.extension_path_user(__package__, create=True))
    except Exception:
        d = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GTAPropImporter"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    p = prefs().output_dir.strip()
    d = Path(bpy.path.abspath(p)) if p else user_dir() / "props"
    d.mkdir(parents=True, exist_ok=True)
    return d


def guess_gta_dir() -> str:
    candidates = []
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidates += [
            Path(root) / "Rockstar Games" / "Grand Theft Auto V",
            Path(root) / "Rockstar Games" / "Grand Theft Auto V Enhanced",
            Path(root) / "Steam" / "steamapps" / "common" / "Grand Theft Auto V",
            Path(root) / "Steam" / "steamapps" / "common" / "Grand Theft Auto V Enhanced",
            Path(root) / "Epic Games" / "GTAV",
            Path(root) / "Epic Games" / "GTAVEnhanced",
        ]
    # Bibliotheques Steam supplementaires
    vdf = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "steamapps" / "libraryfolders.vdf"
    try:
        import re
        for lib in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore")):
            lib = lib.replace("\\\\", "\\")
            for g in ("Grand Theft Auto V", "Grand Theft Auto V Enhanced"):
                candidates.append(Path(lib) / "steamapps" / "common" / g)
    except Exception:
        pass
    for c in candidates:
        if (c / "GTA5.exe").is_file() or (c / "GTA5_Enhanced.exe").is_file():
            return str(c)
    return ""


def sollumz_available() -> bool:
    try:
        bpy.ops.sollumz.import_assets.get_rna_type()
        return True
    except Exception:
        return False


def sollumz_import_settings():
    for key, addon in bpy.context.preferences.addons.items():
        if "sollumz" in key.lower():
            return getattr(addon.preferences, "import_settings", None)
    return None


def tag_redraw():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ---------------------------------------------------------------- compilation de l'outil

def find_csc() -> Path | None:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for fw in ("Framework64", "Framework"):
        p = windir / "Microsoft.NET" / fw / "v4.0.30319" / "csc.exe"
        if p.is_file():
            return p
    return None


def find_netstandard() -> Path | None:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    cands = [
        windir / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "netstandard.dll",
        windir / "Microsoft.NET" / "assembly" / "GAC_MSIL" / "netstandard" / "v4.0_2.0.0.0__cc7b13ffcd2ddd51" / "netstandard.dll",
    ]
    ref = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Reference Assemblies" / "Microsoft" / "Framework" / ".NETFramework"
    if ref.is_dir():
        for v in sorted(ref.glob("v4.*"), reverse=True):
            cands.append(v / "Facades" / "netstandard.dll")
    for c in cands:
        if c.is_file():
            return c
    return None


EXE_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <startup><supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.8"/></startup>
  <runtime>
    <loadFromRemoteSources enabled="true"/>
    <gcAllowVeryLargeObjects enabled="true"/>
  </runtime>
</configuration>
"""


def ensure_tool(cw_dir: Path) -> Path:
    """Compile PropGrabber.exe si besoin. Renvoie le chemin de l'exe ou leve RuntimeError."""
    core = cw_dir / "CodeWalker.Core.dll"
    if not core.is_file():
        raise RuntimeError(L(f"CodeWalker.Core.dll introuvable dans : {cw_dir}", f"CodeWalker.Core.dll not found in: {cw_dir}"))

    out_dir = user_dir() / "tool"
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = out_dir / "PropGrabber.exe"
    stamp_file = out_dir / "build.stamp"

    h = hashlib.sha1()
    h.update(SOURCE_CS.read_bytes())
    h.update(str(core.resolve()).encode())
    h.update(str(core.stat().st_mtime_ns).encode())
    stamp = h.hexdigest()
    if exe.is_file() and stamp_file.is_file() and stamp_file.read_text().strip() == stamp:
        return exe

    csc = find_csc()
    if csc is None:
        raise RuntimeError(L("Compilateur C# de Windows introuvable (.NET Framework 4.8 requis).", "Windows C# compiler not found (.NET Framework 4.8 required)."))
    netstd = find_netstandard()
    if netstd is None:
        raise RuntimeError(L("netstandard.dll introuvable : installe .NET Framework 4.8 (Windows Update).", "netstandard.dll not found: install .NET Framework 4.8 (Windows Update)."))

    refs = [core, netstd] + sorted(cw_dir.glob("SharpDX*.dll"))
    cmd = [str(csc), "/nologo", "/optimize+", "/platform:x64", "/target:exe",
           f"/out:{exe}"] + [f"/reference:{r}" for r in refs] + [str(SOURCE_CS)]
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0 or not exe.is_file():
        log = out_dir / "build.log"
        log.write_text(res.stdout + "\n" + res.stderr, encoding="utf-8")
        first = next((l for l in (res.stdout + res.stderr).splitlines() if "error" in l.lower()), L("erreur inconnue", "unknown error"))
        raise RuntimeError(L(f"Echec de compilation : {first}  (journal : {log})", f"Build failed: {first}  (log: {log})"))
    (out_dir / "PropGrabber.exe.config").write_text(EXE_CONFIG, encoding="utf-8")
    stamp_file.write_text(stamp)
    return exe


# ---------------------------------------------------------------- processus d'extraction (garde vivant)

class Grabber:
    proc: subprocess.Popen | None = None
    lines: "queue.Queue[dict]" = queue.Queue()
    ready = False
    status = ""
    count = 0
    results: list[str] = []
    results_kind = "PROP"   # PROP ou YMAP : ce qu'un clic sur un resultat importe
    last_message = ""
    busy = False
    discard = 0             # reponses a ignorer (operations annulees avec Echap)

    @classmethod
    def alive(cls) -> bool:
        return cls.proc is not None and cls.proc.poll() is None

    @classmethod
    def start(cls):
        if cls.alive():
            return
        p = prefs()
        cw = Path(bpy.path.abspath(p.codewalker_dir.strip()))
        gta = Path(bpy.path.abspath(p.gta_dir.strip()))
        if not p.codewalker_dir.strip():
            raise RuntimeError(L("Indique le dossier de CodeWalker dans les preferences de l'addon.", "Set the CodeWalker folder in the add-on preferences."))
        if not ((gta / "GTA5.exe").is_file() or (gta / "GTA5_Enhanced.exe").is_file()):
            raise RuntimeError(L(f"GTA5.exe introuvable dans : {gta}", f"GTA5.exe not found in: {gta}"))
        cls.status = L("Compilation de l'outil...", "Building the tool...")
        exe = ensure_tool(cw)

        cls.ready = False
        cls.lines = queue.Queue()
        cls.proc = subprocess.Popen(
            [str(exe), str(cw), str(gta)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=cls._reader, args=(cls.proc,), daemon=True).start()
        cls.status = L("Demarrage...", "Starting...")

    @classmethod
    def _reader(cls, proc):
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = {"log": raw}
            cls.lines.put(msg)
        cls.lines.put({"exited": proc.wait()})

    @classmethod
    def send(cls, *fields):
        cls.proc.stdin.write("\t".join(str(f).replace("\t", " ") for f in fields) + "\n")
        cls.proc.stdin.flush()

    @classmethod
    def stop(cls):
        if cls.alive():
            try:
                cls.send("quit")
                cls.proc.wait(timeout=3)
            except Exception:
                cls.proc.kill()
        cls.proc = None
        cls.ready = False
        cls.status = ""


# ---------------------------------------------------------------- preferences

class GTAPI_Prefs(AddonPreferences):
    bl_idname = __package__

    language: bpy.props.EnumProperty(
        name="Langue / Language",
        items=(
            ("FR", "Français", "Interface en français"),
            ("EN", "English", "English interface"),
        ),
        default="FR",
    )
    codewalker_dir: StringProperty(
        name="Dossier CodeWalker",
        description="Dossier qui contient CodeWalker.Core.dll\nFolder that contains CodeWalker.Core.dll",
        subtype="DIR_PATH",
    )
    gta_dir: StringProperty(
        name="Dossier GTA V",
        description="Dossier qui contient GTA5.exe (ou GTA5_Enhanced.exe)\nFolder that contains GTA5.exe (or GTA5_Enhanced.exe)",
        subtype="DIR_PATH",
    )
    output_dir: StringProperty(
        name="Dossier d'extraction",
        description="Ou ranger les modeles et textures extraits (vide = dossier de l'addon)\nWhere to put the extracted models and textures (empty = add-on folder)",
        subtype="DIR_PATH",
    )

    def draw(self, context):
        col = self.layout.column()
        col.row().prop(self, "language", expand=True)
        col.prop(self, "codewalker_dir", text=L("Dossier CodeWalker", "CodeWalker folder"))
        row = col.row(align=True)
        row.prop(self, "gta_dir", text=L("Dossier GTA V", "GTA V folder"))
        row.operator("gtapi.detect_gta", text="", icon="VIEWZOOM")
        col.prop(self, "output_dir", text=L("Dossier d'extraction", "Extraction folder"))
        if not sollumz_available():
            col.label(text=L("Sollumz n'est pas active : il est requis pour l'import.",
                             "Sollumz is not enabled: it is required for importing."), icon="ERROR")


# ---------------------------------------------------------------- proprietes de scene

def base_name(name: str) -> str:
    """prop_bench_01a.003 -> prop_bench_01a"""
    head, dot, tail = name.rpartition(".")
    return head if dot and tail.isdigit() else name


def objects_of_model(model: str):
    model = model.lower()
    for o in bpy.data.objects:
        if o.parent is None and base_name(o.name).lower() == model:
            yield o


def _set_model_visible(model: str, visible: bool):
    for root in objects_of_model(model):
        for o in [root] + list(root.children_recursive):
            o.hide_viewport = not visible
            o.hide_render = not visible


def _on_model_toggle(self, context):
    _set_model_visible(self.name, self.visible)


def match_globs(name: str, globs: str) -> bool:
    import fnmatch
    name = name.lower()
    return any(fnmatch.fnmatchcase(name, g.strip().lower())
               for g in globs.replace(",", ";").replace(" ", ";").split(";") if g.strip())


# ---------------------------------------------------------------- affichage : lumieres et lignes bleues

def sollumz_lights():
    for o in bpy.data.objects:
        if o.type == "LIGHT" and getattr(o, "sollum_type", "") == "sollumz_light":
            yield o


def apply_lights_visibility(scene):
    show = scene.gtapi.show_lights
    n = 0
    for o in sollumz_lights():
        if o.hide_viewport == show or o.hide_render == show:
            o.hide_viewport = not show
            o.hide_render = not show
            n += 1
    return n


def apply_lines_visibility(scene):
    show = scene.gtapi.show_blue_lines
    # Lignes parent/enfant de Blender (pointilles bleus)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                for space in area.spaces:
                    if space.type == "VIEW_3D":
                        space.overlay.show_relationship_lines = show
    # Overlay de hierarchie LOD des ymap (Sollumz)
    wm = bpy.context.window_manager
    for attr in ("sz_ui_map_lod_overlay_show_lines", "sz_ui_map_lod_overlay_show_outlines"):
        if hasattr(wm, attr):
            setattr(wm, attr, show)


def props_show_lines(context):
    return context.scene.gtapi.show_blue_lines


def _on_lights_toggle(self, context):
    apply_lights_visibility(context.scene)


def _on_lines_toggle(self, context):
    apply_lines_visibility(context.scene)


class GTAPI_YmapModel(PropertyGroup):
    name: StringProperty()
    count: IntProperty()
    visible: BoolProperty(name="Visible", default=True, update=_on_model_toggle,
                          description="Afficher ou masquer toutes les copies de ce prop\nShow or hide every copy of this prop")


class GTAPI_Props(PropertyGroup):
    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ("PROP", "Prop", "Importer un seul prop\nImport a single prop", "MESH_CUBE", 0),
            ("YMAP", "YMAP", "Importer un ymap complet : tous ses props, places a leur position dans le monde\nImport a whole ymap: all its props, placed at their world position", "WORLD", 1),
        ),
        default="PROP",
    )
    prop_name: StringProperty(name="Prop", description="Nom du prop, ex. prop_bench_01a\nProp name, e.g. prop_bench_01a")
    ymap_name: StringProperty(name="YMAP", description="Nom du ymap, ex. dt1_02_strm_0\nYmap name, e.g. dt1_02_strm_0")
    ymap_hd_only: BoolProperty(
        name="Modeles HD seulement",
        description="Ignore les modeles LOD/SLOD (versions basse qualite vues de loin). Beaucoup plus rapide\nSkip LOD/SLOD models (low quality versions seen from far away). Much faster",
        default=True,
    )
    ymap_skip_props: BoolProperty(
        name="Desactiver les props",
        description="Ne pas importer les modeles dont le nom correspond au filtre (les entites restent dans le ymap, sans modele)\nDo not import models whose name matches the filter (the entities stay in the ymap, without a model)",
        default=False,
    )
    ymap_exclude: StringProperty(
        name="Filtre",
        description="Modeles a ne pas importer, separes par ; (jokers * et ?)\nModels not to import, separated by ; (wildcards * and ?)",
        default="prop_*;p_*",
    )
    ymap_models: bpy.props.CollectionProperty(type=GTAPI_YmapModel)
    ymap_models_index: IntProperty()
    ymap_list_filter: StringProperty(name="Filtrer la liste", description="N'afficher que les modeles contenant ce texte\nOnly show models containing this text")
    place_at_cursor: BoolProperty(name="Placer au curseur 3D", default=True)
    show_lights: BoolProperty(
        name="Lumieres",
        description="Afficher les lumieres des props GTA (elles restent dans le modele, seulement masquees)\nShow the GTA prop lights (they stay in the model, only hidden)",
        default=True,
        update=_on_lights_toggle,
    )
    show_blue_lines: BoolProperty(
        name="Lignes bleues",
        description="Afficher les lignes parent/enfant et les lignes de hierarchie LOD des ymap\nShow parent/child lines and the ymap LOD hierarchy lines",
        default=True,
        update=_on_lines_toggle,
    )
    search_limit: IntProperty(name="Resultats max", default=30, min=5, max=200)


# ---------------------------------------------------------------- operateurs

class GTAPI_OT_detect_gta(Operator):
    bl_idname = "gtapi.detect_gta"
    bl_label = "Detecter GTA V"
    bl_description = "Cherche le dossier d'installation de GTA V"

    @classmethod
    def description(cls, context, properties):
        return L("Cherche le dossier d'installation de GTA V", "Find the GTA V install folder")

    def execute(self, context):
        g = guess_gta_dir()
        if g:
            prefs().gta_dir = g
            self.report({"INFO"}, L(f"GTA V trouve : {g}", f"GTA V found: {g}"))
        else:
            self.report({"WARNING"}, L("GTA V introuvable automatiquement, choisis le dossier a la main.", "GTA V not found automatically, pick the folder by hand."))
        return {"FINISHED"}


class GTAPI_OT_run(Operator):
    """Envoie une commande a l'outil d'extraction sans bloquer Blender"""
    bl_idname = "gtapi.run"
    bl_label = "GTA Prop"
    bl_options = {"REGISTER", "UNDO"}

    action: StringProperty(default="import", options={"HIDDEN", "SKIP_SAVE"})  # import | search | ymap | searchymap | warmup
    name: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    _timer = None
    _sent = False

    @classmethod
    def description(cls, context, properties):
        return {
            "import": L("Extraire ce prop de GTA V et l'importer avec ses textures",
                        "Extract this prop from GTA V and import it with its textures"),
            "search": L("Chercher les props dont le nom contient ce texte", "Search props whose name contains this text"),
            "ymap": L("Extraire ce ymap et tous ses props, puis les placer a leur position",
                      "Extract this ymap and all its props, then place them at their position"),
            "searchymap": L("Chercher les ymap dont le nom contient ce texte", "Search ymaps whose name contains this text"),
            "warmup": L("Lancer le scan du jeu maintenant (1 a 2 minutes la premiere fois)",
                        "Scan the game now (1-2 minutes the first time)"),
        }.get(properties.action, "")

    def invoke(self, context, event):
        if Grabber.busy:
            self.report({"WARNING"}, L("Une operation est deja en cours, patiente.", "An operation is already running, please wait."))
            return {"CANCELLED"}
        if self.action in {"import", "ymap"} and not sollumz_available():
            self.report({"ERROR"}, L("Sollumz doit etre installe et active.", "Sollumz must be installed and enabled."))
            return {"CANCELLED"}

        props = context.scene.gtapi
        is_ymap = self.action in {"ymap", "searchymap"}
        if not self.name:
            self.name = (props.ymap_name if is_ymap else props.prop_name).strip()
        if self.action != "warmup" and not self.name:
            self.report({"WARNING"}, L("Tape un nom de ymap.", "Type a ymap name.") if is_ymap else L("Tape un nom de prop.", "Type a prop name."))
            return {"CANCELLED"}
        if self.action == "import":
            props.prop_name = self.name
        elif self.action == "ymap":
            props.ymap_name = self.name

        try:
            Grabber.start()
        except Exception as ex:
            Grabber.status = ""
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}

        Grabber.busy = True
        self._sent = False
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.15, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def finish(self, context, result="FINISHED"):
        context.window_manager.event_timer_remove(self._timer)
        Grabber.busy = False
        context.workspace.status_text_set(None)
        tag_redraw()
        return {result}

    def modal(self, context, event):
        if event.type == "ESC":
            if self._sent:
                Grabber.discard += 1  # l'outil finira quand meme : on ignorera sa reponse
            Grabber.status = L("Annule", "Cancelled")
            return self.finish(context, "CANCELLED")
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        while True:
            try:
                msg = Grabber.lines.get_nowait()
            except queue.Empty:
                break

            if "status" in msg:
                Grabber.status = tool_msg(msg["status"])
            elif "ready" in msg:
                Grabber.ready = True
                Grabber.count = msg.get("count", 0)
                Grabber.status = L(f"Pret - {Grabber.count} modeles indexes", f"Ready - {Grabber.count} models indexed")
            elif "exited" in msg:
                Grabber.proc = None
                Grabber.ready = False
                self.report({"ERROR"}, L("L'outil d'extraction s'est arrete. ", "The extraction tool stopped. ") + tool_msg(Grabber.last_message))
                return self.finish(context, "CANCELLED")
            elif "error" in msg and "ok" not in msg:
                Grabber.last_message = msg["error"]
                Grabber.status = L("Erreur : ", "Error: ") + tool_msg(msg["error"])
                if not Grabber.ready or self._sent:
                    self.report({"ERROR"}, tool_msg(msg["error"]))
                    return self.finish(context, "CANCELLED")
            elif "log" in msg:
                Grabber.last_message = msg["log"]
            elif ("results" in msg or "ok" in msg) and Grabber.discard > 0:
                Grabber.discard -= 1  # reponse d'une operation annulee
            elif "results" in msg:
                Grabber.results = msg["results"]
                Grabber.results_kind = "YMAP" if self.action == "searchymap" else "PROP"
                Grabber.status = L(f"{len(Grabber.results)} resultat(s) pour \u00ab {self.name} \u00bb", f"{len(Grabber.results)} result(s) for \u201c{self.name}\u201d")
                return self.finish(context)
            elif "ok" in msg:
                if self.action == "ymap":
                    return self.on_ymap(context, msg)
                return self.on_export(context, msg)

        if Grabber.ready and not self._sent:
            if self.action == "warmup":
                return self.finish(context)
            if self.action == "search":
                Grabber.send("search", self.name, context.scene.gtapi.search_limit)
            elif self.action == "searchymap":
                Grabber.send("searchymap", self.name, context.scene.gtapi.search_limit)
            elif self.action == "ymap":
                Grabber.status = L(f"Lecture du ymap {self.name}...", f"Reading ymap {self.name}...")
                pr = context.scene.gtapi
                Grabber.send("ymap", self.name, output_dir(), 1 if pr.ymap_hd_only else 0,
                             pr.ymap_exclude if pr.ymap_skip_props else "")
            else:
                Grabber.status = L(f"Extraction de {self.name}...", f"Extracting {self.name}...")
                Grabber.send("export", self.name, output_dir())
            self._sent = True

        context.workspace.status_text_set(f"GTA Prop : {Grabber.status}   " + L("(Echap pour annuler)", "(Esc to cancel)"))
        tag_redraw()
        return {"RUNNING_MODAL"}

    def on_export(self, context, msg):
        if not msg.get("ok"):
            sugg = msg.get("suggestions") or []
            if sugg:
                Grabber.results = sugg
                Grabber.results_kind = "PROP"
            Grabber.status = tool_msg(msg.get("error")) or L("Echec", "Failed")
            self.report({"ERROR"}, Grabber.status + (L(" - voir les suggestions", " - see the suggestions") if sugg else ""))
            return self.finish(context, "CANCELLED")

        before = set(bpy.data.objects)
        try:
            bpy.ops.sollumz.import_assets(directory=msg["dir"] + os.sep, files=[{"name": msg["file"]}])
        except Exception as ex:
            self.report({"ERROR"}, L(f"Import Sollumz echoue : {ex}", f"Sollumz import failed: {ex}"))
            return self.finish(context, "CANCELLED")
        new_objs = [o for o in bpy.data.objects if o not in before]

        props = context.scene.gtapi
        roots = [o for o in new_objs if o.parent is None or o.parent not in new_objs]
        if props.place_at_cursor:
            for o in roots:
                o.location = context.scene.cursor.location
        if roots:
            for o in context.selected_objects:
                o.select_set(False)
            for o in roots:
                try:
                    o.select_set(True)
                except RuntimeError:
                    pass
            context.view_layer.objects.active = roots[0]

        apply_lights_visibility(context.scene)
        missing = msg.get("missing") or []
        Grabber.status = L(f"{msg['file']} importe ({msg['textures']} textures)", f"{msg['file']} imported ({msg['textures']} textures)")
        if missing:
            Grabber.status += L(f", {len(missing)} manquante(s)", f", {len(missing)} missing")
            self.report({"WARNING"}, L("Textures introuvables : ", "Textures not found: ") + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
        else:
            self.report({"INFO"}, Grabber.status)
        return self.finish(context)


    def on_ymap(self, context, msg):
        if not msg.get("ok"):
            sugg = msg.get("suggestions") or []
            if sugg:
                Grabber.results = sugg
                Grabber.results_kind = "YMAP"
            Grabber.status = tool_msg(msg.get("error")) or L("Echec", "Failed")
            self.report({"ERROR"}, Grabber.status + (L(" - voir les suggestions", " - see the suggestions") if sugg else ""))
            return self.finish(context, "CANCELLED")

        # 1) Importe chaque modele different (a l'origine), sauf ceux deja presents dans le .blend
        existing = {o.name.lower() for o in bpy.data.objects}
        to_import = [f for f in msg.get("models", []) if f.split(".")[0].lower() not in existing]
        if to_import:
            Grabber.status = L(f"Import de {len(to_import)} modeles dans Blender...", f"Importing {len(to_import)} models into Blender...")
            context.workspace.status_text_set(f"GTA Prop : {Grabber.status}")
            try:
                bpy.ops.sollumz.import_assets(directory=msg["dir"] + os.sep, files=[{"name": f} for f in to_import])
            except Exception as ex:
                self.report({"ERROR"}, L(f"Import des modeles echoue : {ex}", f"Model import failed: {ex}"))
                return self.finish(context, "CANCELLED")

        # 2) Importe le ymap : Sollumz place chaque entite sur le modele du meme nom
        settings = sollumz_import_settings()
        old = getattr(settings, "ymap_instance_entities", None) if settings else None
        try:
            if old is not None:
                settings.ymap_instance_entities = True
            bpy.ops.sollumz.import_assets(directory=msg["ymapdir"] + os.sep, files=[{"name": msg["ymap"]}])
        except Exception as ex:
            self.report({"ERROR"}, L(f"Import du ymap echoue : {ex}", f"Ymap import failed: {ex}"))
            return self.finish(context, "CANCELLED")
        finally:
            if old is not None:
                settings.ymap_instance_entities = old

        apply_lights_visibility(context.scene)
        if not props_show_lines(context):
            apply_lines_visibility(context.scene)

        # 3) Liste des modeles places, pour pouvoir les masquer un par un
        model_names = {f.split(".")[0].lower() for f in msg.get("models", [])}
        counts = {}
        for o in bpy.data.objects:
            if o.parent is not None:
                continue
            b = base_name(o.name).lower()
            if b in model_names:
                counts[b] = counts.get(b, 0) + 1
        props = context.scene.gtapi
        props.ymap_models.clear()
        for name in sorted(counts):
            it = props.ymap_models.add()
            it.name = name
            it.count = counts[name]

        failed = msg.get("failed") or []
        nmod = len(msg.get('models', []))
        Grabber.status = L(f"{msg['ymap']} importe : {msg['entities']} entites, {nmod} modeles",
                           f"{msg['ymap']} imported: {msg['entities']} entities, {nmod} models")
        notes = []
        if msg.get("skippedlod"):
            notes.append(L(f"{msg['skippedlod']} entites LOD ignorees", f"{msg['skippedlod']} LOD entities skipped"))
        if msg.get("excluded"):
            notes.append(L(f"{msg['excluded']} entites desactivees par le filtre", f"{msg['excluded']} entities disabled by the filter"))
        if msg.get("mlo"):
            notes.append(L(f"{msg['mlo']} interieur(s) MLO non importe(s)", f"{msg['mlo']} MLO interior(s) not imported"))
        if failed:
            notes.append(L(f"{len(failed)} modele(s) introuvable(s) : ", f"{len(failed)} model(s) not found: ") + ", ".join(failed[:6]) + (" ..." if len(failed) > 6 else ""))
        self.report({"WARNING"} if failed else {"INFO"}, Grabber.status + ("  |  " + " ; ".join(notes) if notes else ""))
        return self.finish(context)


class GTAPI_OT_models_visibility(Operator):
    bl_idname = "gtapi.models_visibility"
    bl_label = "Visibilite des props"
    bl_description = "Afficher ou masquer plusieurs modeles du ymap d'un coup"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def description(cls, context, properties):
        return {
            "SHOW_ALL": L("Afficher tous les modeles du ymap", "Show all ymap models"),
            "HIDE_ALL": L("Masquer tous les modeles du ymap", "Hide all ymap models"),
        }.get(properties.mode, L("Masquer les modeles qui correspondent au filtre", "Hide the models that match the filter"))

    mode: StringProperty(default="SHOW_ALL", options={"HIDDEN", "SKIP_SAVE"})  # SHOW_ALL | HIDE_ALL | HIDE_FILTER

    def execute(self, context):
        props = context.scene.gtapi
        n = 0
        for it in props.ymap_models:
            if self.mode == "SHOW_ALL":
                want = True
            elif self.mode == "HIDE_ALL":
                want = False
            else:
                want = it.visible and not match_globs(it.name, props.ymap_exclude)
            if it.visible != want:
                it.visible = want  # declenche _on_model_toggle
                n += 1
        self.report({"INFO"}, L(f"{n} modele(s) modifie(s)", f"{n} model(s) changed"))
        return {"FINISHED"}


class GTAPI_OT_stop(Operator):
    bl_idname = "gtapi.stop"
    bl_label = "Arreter l'outil"
    bl_description = "Ferme l'outil d'extraction et libere sa memoire"

    @classmethod
    def description(cls, context, properties):
        return L("Ferme l'outil d'extraction et libere sa memoire", "Close the extraction tool and free its memory")

    def execute(self, context):
        Grabber.stop()
        tag_redraw()
        return {"FINISHED"}


class GTAPI_OT_open_folder(Operator):
    bl_idname = "gtapi.open_folder"
    bl_label = "Ouvrir le dossier d'extraction"
    bl_description = "Ouvre le dossier ou les modeles et textures sont extraits"

    @classmethod
    def description(cls, context, properties):
        return L("Ouvre le dossier ou les modeles et textures sont extraits", "Open the folder where models and textures are extracted")

    def execute(self, context):
        os.startfile(str(output_dir())) if sys.platform == "win32" else None
        return {"FINISHED"}


# ---------------------------------------------------------------- panneau

class GTAPI_UL_models(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "visible", text="", icon="HIDE_OFF" if item.visible else "HIDE_ON", emboss=False)
        row.label(text=item.name)
        row.label(text=f"x{item.count}")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        f = context.scene.gtapi.ymap_list_filter.strip().lower()
        flags = [self.bitflag_filter_item if (not f or f in it.name.lower()) else 0 for it in items]
        return flags, []


class GTAPI_PT_panel(Panel):
    bl_label = "GTA Prop Importer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Sollumz Tools"
    bl_order = 100

    def draw(self, context):
        layout = self.layout
        props = context.scene.gtapi
        p = prefs()

        layout.row(align=True).prop(p, "language", expand=True)
        if not sollumz_available():
            layout.label(text=L("Active Sollumz d'abord", "Enable Sollumz first"), icon="ERROR")
        if not p.codewalker_dir or not p.gta_dir:
            box = layout.box()
            box.label(text=L("Configuration requise :", "Setup required:"), icon="PREFERENCES")
            box.prop(p, "codewalker_dir", text="CodeWalker")
            row = box.row(align=True)
            row.prop(p, "gta_dir", text="GTA V")
            row.operator("gtapi.detect_gta", text="", icon="VIEWZOOM")

        vis = layout.box()
        vis.label(text=L("Affichage", "Display"), icon="HIDE_OFF")
        r = vis.row(align=True)
        nl = sum(1 for _ in sollumz_lights())
        lights = L("Lumieres", "Lights")
        r.prop(props, "show_lights", text=f"{lights} ({nl})" if nl else lights,
               icon="LIGHT" if props.show_lights else "OUTLINER_DATA_LIGHT", toggle=True)
        r.prop(props, "show_blue_lines", text=L("Lignes bleues", "Blue lines"), icon="IPO_LINEAR" if props.show_blue_lines else "HIDE_ON", toggle=True)

        layout.row().prop(props, "mode", expand=True)
        ymap = props.mode == "YMAP"

        col = layout.column(align=True)
        if ymap:
            col.prop(props, "ymap_name", text="", icon="WORLD")
        else:
            col.prop(props, "prop_name", text="", icon="MESH_CUBE")
        row = col.row(align=True)
        row.scale_y = 1.3
        op = row.operator("gtapi.run", text=L("Importer", "Import"), icon="IMPORT")
        op.action = "ymap" if ymap else "import"
        op = row.operator("gtapi.run", text=L("Chercher", "Search"), icon="VIEWZOOM")
        op.action = "searchymap" if ymap else "search"
        if ymap:
            layout.prop(props, "ymap_hd_only", text=L("Modeles HD seulement", "HD models only"))
            layout.prop(props, "ymap_skip_props", text=L("Desactiver les props", "Disable props"))
            sub = layout.row()
            sub.prop(props, "ymap_exclude", text=L("Filtre", "Filter"))
        else:
            layout.prop(props, "place_at_cursor", text=L("Placer au curseur 3D", "Place at 3D cursor"))

        box = layout.box()
        if Grabber.alive():
            box.label(text=Grabber.status or L("En marche", "Running"), icon="CHECKMARK" if Grabber.ready else "TIME")
            box.operator("gtapi.stop", text=L("Arreter l'outil", "Stop the tool"), icon="CANCEL")
        else:
            box.label(text=Grabber.status or L("Outil arrete (demarre au premier import)", "Tool stopped (starts on first import)"), icon="INFO")
            op = box.operator("gtapi.run", text=L("Scanner le jeu maintenant", "Scan the game now"), icon="PLAY")
            op.action = "warmup"

        if Grabber.results:
            res = layout.box()
            res.label(text=L("Resultats (clic = importer) :", "Results (click = import):"))
            c = res.column(align=True)
            is_y = Grabber.results_kind == "YMAP"
            for n in Grabber.results:
                op = c.operator("gtapi.run", text=n, icon="WORLD" if is_y else "OBJECT_DATA")
                op.action = "ymap" if is_y else "import"
                op.name = n

        if ymap and len(props.ymap_models):
            mb = layout.box()
            hidden = sum(1 for it in props.ymap_models if not it.visible)
            mb.label(text=L(f"Props du ymap ({len(props.ymap_models)}, {hidden} masque(s))", f"Ymap props ({len(props.ymap_models)}, {hidden} hidden)"), icon="OUTLINER")
            r = mb.row(align=True)
            op = r.operator("gtapi.models_visibility", text=L("Tout", "All"), icon="HIDE_OFF")
            op.mode = "SHOW_ALL"
            op = r.operator("gtapi.models_visibility", text=L("Aucun", "None"), icon="HIDE_ON")
            op.mode = "HIDE_ALL"
            op = r.operator("gtapi.models_visibility", text=L("Masquer filtre", "Hide filter"), icon="FILTER")
            op.mode = "HIDE_FILTER"
            mb.prop(props, "ymap_list_filter", text="", icon="VIEWZOOM")
            mb.template_list("GTAPI_UL_models", "", props, "ymap_models", props, "ymap_models_index", rows=6)

        layout.operator("gtapi.open_folder", text=L("Ouvrir le dossier d'extraction", "Open extraction folder"), icon="FILE_FOLDER")


# ---------------------------------------------------------------- enregistrement

classes = (
    GTAPI_Prefs,
    GTAPI_YmapModel,
    GTAPI_Props,
    GTAPI_OT_detect_gta,
    GTAPI_OT_run,
    GTAPI_OT_models_visibility,
    GTAPI_OT_stop,
    GTAPI_OT_open_folder,
    GTAPI_UL_models,
    GTAPI_PT_panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.gtapi = bpy.props.PointerProperty(type=GTAPI_Props)
    try:
        p = prefs()
        if not p.gta_dir:
            p.gta_dir = guess_gta_dir()
    except Exception:
        pass


def unregister():
    Grabber.stop()
    del bpy.types.Scene.gtapi
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
