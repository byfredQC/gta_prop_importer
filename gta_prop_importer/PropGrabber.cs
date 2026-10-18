// PropGrabber - extrait un prop GTA V (modele + textures) pour Sollumz/Blender.
// Compile automatiquement par l'addon avec le csc.exe de Windows (.NET Framework 4.8),
// et charge le CodeWalker.Core.dll de ton install CodeWalker.
//
// Usage :  PropGrabber.exe <dossier CodeWalker> <dossier GTA V>
// Commandes sur stdin (une par ligne, champs separes par TAB) :
//   export<TAB>prop_bench_01a<TAB>C:\dossier\sortie
//   search<TAB>bench<TAB>50
//   ymap<TAB>nom_du_ymap<TAB>C:\dossier\sortie<TAB>1<TAB>prop_*;p_*
//        (1 = modeles HD seulement ; 5e champ = modeles a ne pas extraire, jokers * et ?)
//   searchymap<TAB>dt1_02<TAB>50
//   quit
// Reponses : une ligne JSON par commande sur stdout.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.RegularExpressions;
using CodeWalker.GameFiles;
using CodeWalker.Utils;

namespace PropGrabber
{
    internal static class Program
    {
        static string cwDir;
        static TextWriter outw;

        static int Main(string[] args)
        {
            outw = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false));
            ((StreamWriter)outw).AutoFlush = true;
            Console.SetOut(TextWriter.Null); // stdout reserve au protocole

            if (args.Length < 2)
            {
                Send("{\"error\":" + Js("Usage : PropGrabber.exe <dossier CodeWalker> <dossier GTA V>") + "}");
                return 1;
            }
            cwDir = args[0].TrimEnd('\\', '/');
            AppDomain.CurrentDomain.AssemblyResolve += ResolveFromCodeWalker;
            Directory.SetCurrentDirectory(cwDir);

            try
            {
                return Server.Run(args[1].TrimEnd('\\', '/'));
            }
            catch (Exception ex)
            {
                Send("{\"error\":" + Js(ex.GetType().Name + " : " + ex.Message) + "}");
                return 1;
            }
        }

        static Assembly ResolveFromCodeWalker(object sender, ResolveEventArgs e)
        {
            string name = new AssemblyName(e.Name).Name;
            string p = Path.Combine(cwDir, name + ".dll");
            return File.Exists(p) ? Assembly.LoadFrom(p) : null;
        }

        internal static void Send(string json)
        {
            outw.WriteLine(json);
        }

        internal static string Js(string s)
        {
            if (s == null) return "null";
            var sb = new StringBuilder("\"");
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 32) sb.Append("\\u" + ((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }

        internal static string JsList(IEnumerable<string> items)
        {
            return "[" + string.Join(",", items.Select(Js)) + "]";
        }
    }

    // Classe separee : CodeWalker.Core n'est charge qu'ici, apres l'installation du AssemblyResolve.
    internal static class Server
    {
        static GameFileCache gfc;
        static List<string> allNames = new List<string>();
        static List<string> ymapNames = new List<string>();
        static readonly Regex TexParamRx = new Regex("type=\"Texture\">\\s*<Name>([^<]*)</Name>", RegexOptions.Compiled);

        [MethodImpl(MethodImplOptions.NoInlining)]
        internal static int Run(string gta)
        {
            if (!Directory.Exists(gta))
            {
                Program.Send("{\"error\":" + Program.Js("Dossier GTA V introuvable : " + gta) + "}");
                return 1;
            }
            bool gen9 = !File.Exists(Path.Combine(gta, "GTA5.exe")) && File.Exists(Path.Combine(gta, "GTA5_Enhanced.exe"));

            Init(gta, gen9);
            Program.Send("{\"ready\":true,\"count\":" + allNames.Count + "}");

            var stdin = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
            string line;
            while ((line = stdin.ReadLine()) != null)
            {
                line = line.TrimEnd('\r');
                if (line.Trim().Length == 0) continue;
                string[] p = line.Split('\t');
                try
                {
                    switch (p[0])
                    {
                        case "export":
                            Program.Send(Export(p.Length > 1 ? p[1] : "", p.Length > 2 ? p[2] : "."));
                            break;
                        case "search":
                            int limit = 50;
                            if (p.Length > 2) int.TryParse(p[2], out limit);
                            Program.Send("{\"results\":" + Program.JsList(Search(allNames, p.Length > 1 ? p[1] : "", limit)) + "}");
                            break;
                        case "searchymap":
                            int ylimit = 50;
                            if (p.Length > 2) int.TryParse(p[2], out ylimit);
                            Program.Send("{\"results\":" + Program.JsList(Search(ymapNames, p.Length > 1 ? p[1] : "", ylimit)) + "}");
                            break;
                        case "ymap":
                            Program.Send(ExportYmap(p.Length > 1 ? p[1] : "", p.Length > 2 ? p[2] : ".", p.Length <= 3 || p[3] != "0", p.Length > 4 ? p[4] : ""));
                            break;
                        case "ping":
                            Program.Send("{\"pong\":true}");
                            break;
                        case "quit":
                            return 0;
                        default:
                            Program.Send("{\"error\":" + Program.Js("Commande inconnue : " + p[0]) + "}");
                            break;
                    }
                }
                catch (Exception ex)
                {
                    Program.Send("{\"ok\":false,\"error\":" + Program.Js(ex.GetType().Name + " : " + ex.Message) + "}");
                }
            }
            return 0;
        }

        static void Status(string s)
        {
            Program.Send("{\"status\":" + Program.Js(s) + "}");
        }

        // --- Initialisation, par reflexion pour marcher avec les anciennes et nouvelles versions de CodeWalker ---

        static void LoadKeys(string gta, bool gen9)
        {
            Status("Chargement des cles du jeu...");
            MethodInfo m = typeof(GTA5Keys).GetMethods(BindingFlags.Public | BindingFlags.Static)
                .Where(x => x.Name == "LoadFromPath").OrderByDescending(x => x.GetParameters().Length).First();
            m.Invoke(null, BuildArgs(m.GetParameters(), gta, gen9));
        }

        static object[] BuildArgs(ParameterInfo[] ps, string gta, bool gen9)
        {
            var a = new object[ps.Length];
            for (int i = 0; i < ps.Length; i++)
            {
                string n = ps[i].Name.ToLowerInvariant();
                Type t = ps[i].ParameterType;
                if (n == "path" || n == "folder") a[i] = gta;
                else if (n == "gen9") a[i] = gen9;
                else if (n == "size") a[i] = 2L * 1024 * 1024 * 1024;
                else if (n == "cachetime") a[i] = 3600.0;
                else if (n == "dlc") a[i] = "";
                else if (n == "mods") a[i] = false;
                else if (n == "excludefolders") a[i] = "Installers;_CommonRedist";
                else if (ps[i].HasDefaultValue) a[i] = ps[i].DefaultValue;
                else a[i] = t.IsValueType ? Activator.CreateInstance(t) : null;
            }
            return a;
        }

        static void SetMember(object o, string name, object value)
        {
            Type t = o.GetType();
            PropertyInfo pi = t.GetProperty(name);
            if (pi != null && pi.CanWrite) { pi.SetValue(o, value, null); return; }
            FieldInfo fi = t.GetField(name);
            if (fi != null) fi.SetValue(o, value);
        }

        static void Init(string gta, bool gen9)
        {
            LoadKeys(gta, gen9);

            ConstructorInfo ctor = typeof(GameFileCache).GetConstructors().OrderByDescending(c => c.GetParameters().Length).First();
            gfc = (GameFileCache)ctor.Invoke(BuildArgs(ctor.GetParameters(), gta, gen9));
            SetMember(gfc, "EnableDlc", true);      // inclut les props des DLC
            SetMember(gfc, "LoadVehicles", false);  // inutile ici, accelere le demarrage
            SetMember(gfc, "LoadPeds", false);
            SetMember(gfc, "LoadAudio", false);

            Status("Scan des archives RPF (1 a 2 minutes la premiere fois)...");
            gfc.Init(new Action<string>(Status), new Action<string>(s => { }));

            var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var e in gfc.YdrDict.Values) set.Add(e.GetShortName().ToLowerInvariant());
            foreach (var e in gfc.YftDict.Values)
            {
                string n = e.GetShortName().ToLowerInvariant();
                if (!n.EndsWith("_hi")) set.Add(n);
            }
            allNames = set.OrderBy(s => s).ToList();

            var yset = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var e in gfc.AllYmapsDict.Values) yset.Add(e.GetShortName().ToLowerInvariant());
            ymapNames = yset.OrderBy(s => s).ToList();
        }

        static List<string> Search(List<string> names, string q, int limit)
        {
            q = (q ?? "").Trim().ToLowerInvariant();
            if (q.Length == 0) return new List<string>();
            var starts = names.Where(n => n.StartsWith(q));
            var contains = names.Where(n => !n.StartsWith(q) && n.Contains(q));
            return starts.Concat(contains).Take(limit).ToList();
        }

        static uint HdHash(uint h)
        {
            MethodInfo m = typeof(GameFileCache).GetMethod("TryGetHDTextureHash");
            if (m == null) return h;
            try { return (uint)m.Invoke(gfc, new object[] { h }); } catch { return h; }
        }

        // --- Export ---

        static string Export(string name, string outDir)
        {
            name = (name ?? "").Trim().ToLowerInvariant();
            foreach (string ext0 in new[] { ".ydr", ".yft", ".ydd" })
                if (name.EndsWith(ext0)) name = name.Substring(0, name.Length - 4);
            if (name.Length == 0) return "{\"ok\":false,\"error\":\"Nom vide\"}";
            return ExportHash(JenkHash.GenHash(name), name, outDir);
        }

        static string ExportHash(uint hash, string name, string outDir)
        {
            Directory.CreateDirectory(outDir);
            Archetype arch = gfc.GetArchetype(hash);

            string baseName, xml, ext, kind, dummy;
            RpfFileEntry ydrEntry = gfc.GetYdrEntry(hash);
            RpfFileEntry yftEntry = gfc.GetYftEntry(hash);
            RpfFileEntry yddEntry = (arch != null && (uint)arch.DrawableDict != 0) ? gfc.GetYddEntry(arch.DrawableDict) : null;

            if (ydrEntry != null)
            {
                YdrFile ydr = gfc.RpfMan.GetFile<YdrFile>(ydrEntry);
                if (ydr == null) return Fail("Impossible de lire " + ydrEntry.Path);
                baseName = ydrEntry.GetShortName().ToLowerInvariant();
                xml = MetaXml.GetXml(ydr, out dummy, Path.Combine(outDir, baseName));
                ext = ".ydr.xml"; kind = "ydr";
            }
            else if (yftEntry != null)
            {
                YftFile yft = gfc.RpfMan.GetFile<YftFile>(yftEntry);
                if (yft == null) return Fail("Impossible de lire " + yftEntry.Path);
                baseName = yftEntry.GetShortName().ToLowerInvariant();
                xml = MetaXml.GetXml(yft, out dummy, Path.Combine(outDir, baseName));
                ext = ".yft.xml"; kind = "yft";
            }
            else if (yddEntry != null)
            {
                YddFile ydd = gfc.RpfMan.GetFile<YddFile>(yddEntry);
                if (ydd == null) return Fail("Impossible de lire " + yddEntry.Path);
                baseName = yddEntry.GetShortName().ToLowerInvariant();
                xml = MetaXml.GetXml(ydd, out dummy, Path.Combine(outDir, baseName));
                ext = ".ydd.xml"; kind = "ydd";
            }
            else
            {
                return "{\"ok\":false,\"error\":" + Program.Js("Prop introuvable : " + name)
                    + ",\"suggestions\":" + Program.JsList(Search(allNames, name, 10)) + "}";
            }

            File.WriteAllText(Path.Combine(outDir, baseName + ext), xml, new UTF8Encoding(false));

            // Textures demandees par les shaders du modele
            string texDir = Path.Combine(outDir, baseName);
            Directory.CreateDirectory(texDir);
            var wanted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (Match m in TexParamRx.Matches(xml))
            {
                string t = m.Groups[1].Value.Trim();
                if (t.Length > 0 && t.ToLowerInvariant() != "givemechecker") wanted.Add(t);
            }
            var missing = new HashSet<string>(wanted.Where(t => !File.Exists(Path.Combine(texDir, t + ".dds"))), StringComparer.OrdinalIgnoreCase);
            int embedded = wanted.Count - missing.Count;

            // Ordre de recherche : dict HD, dict du prop, puis toute la chaine des dicts parents
            var txds = new List<uint>();
            uint txd = arch != null ? (uint)arch.TextureDict : 0;
            AddTxd(txds, txd != 0 ? txd : hash);
            if (txd != 0 && txd != hash) AddTxd(txds, hash);
            for (int i = 0; i < txds.Count && i < 40; i++)
            {
                uint parent = gfc.TryGetParentYtdHash(txds[i]);
                if (parent != 0) AddTxd(txds, parent);
            }

            var usedDicts = new List<string>();
            foreach (uint h in txds)
            {
                if (missing.Count == 0) break;
                RpfFileEntry ytdEntry = gfc.GetYtdEntry(h);
                if (ytdEntry == null) continue;
                YtdFile ytd;
                try { ytd = gfc.RpfMan.GetFile<YtdFile>(ytdEntry); } catch { continue; }
                if (ytd == null || ytd.TextureDict == null || ytd.TextureDict.Textures == null || ytd.TextureDict.Textures.data_items == null) continue;
                bool used = false;
                foreach (Texture tex in ytd.TextureDict.Textures.data_items)
                {
                    if (tex == null || tex.Name == null || !missing.Contains(tex.Name)) continue;
                    try
                    {
                        File.WriteAllBytes(Path.Combine(texDir, tex.Name + ".dds"), DDSIO.GetDDSFile(tex));
                        missing.Remove(tex.Name);
                        used = true;
                    }
                    catch { }
                }
                if (used) usedDicts.Add(ytdEntry.Name);
            }

            return "{\"ok\":true,\"kind\":" + Program.Js(kind)
                + ",\"file\":" + Program.Js(baseName + ext)
                + ",\"dir\":" + Program.Js(outDir)
                + ",\"textures\":" + (wanted.Count - missing.Count)
                + ",\"embedded\":" + embedded
                + ",\"dicts\":" + Program.JsList(usedDicts)
                + ",\"missing\":" + Program.JsList(missing) + "}";
        }

        // --- Export d'un ymap : le .ymap.xml + chaque modele different qu'il place ---

        static Regex GlobsToRegex(string globs)
        {
            var parts = (globs ?? "").Split(new[] { ';', ',', ' ' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(g => "^" + Regex.Escape(g.Trim().ToLowerInvariant()).Replace("\\*", ".*").Replace("\\?", ".") + "$")
                .ToList();
            return parts.Count == 0 ? null : new Regex(string.Join("|", parts), RegexOptions.Compiled);
        }

        static string ExportYmap(string name, string outDir, bool hdOnly, string exclude)
        {
            Regex excludeRx = GlobsToRegex(exclude);
            name = (name ?? "").Trim().ToLowerInvariant();
            if (name.EndsWith(".xml")) name = name.Substring(0, name.Length - 4);
            if (name.EndsWith(".ymap")) name = name.Substring(0, name.Length - 5);
            if (name.Length == 0) return Fail("Nom vide");

            RpfFileEntry entry = gfc.GetYmapEntry(JenkHash.GenHash(name));
            if (entry == null)
            {
                return "{\"ok\":false,\"error\":" + Program.Js("Ymap introuvable : " + name)
                    + ",\"suggestions\":" + Program.JsList(Search(ymapNames, name, 10)) + "}";
            }
            YmapFile ymap = gfc.RpfMan.GetFile<YmapFile>(entry);
            if (ymap == null) return Fail("Impossible de lire " + entry.Path);

            string baseName = entry.GetShortName().ToLowerInvariant();
            string ymapDir = Path.Combine(outDir, "_ymap_" + baseName);
            Directory.CreateDirectory(ymapDir);
            string dummy;
            string xml = MetaXml.GetXml(ymap, out dummy);
            string ymapFile = baseName + ".ymap.xml";
            File.WriteAllText(Path.Combine(ymapDir, ymapFile), xml, new UTF8Encoding(false));

            // Archetypes differents utilises par les entites
            var hashes = new List<uint>();
            int entityCount = 0, skippedLod = 0, skippedExcluded = 0;
            var excludedNames = new List<string>();
            if (ymap.CEntityDefs != null)
            {
                foreach (CEntityDef ent in ymap.CEntityDefs)
                {
                    entityCount++;
                    int lod = (int)ent.lodLevel;
                    if (hdOnly && lod != 0 && lod != 5) { skippedLod++; continue; } // 0 = HD, 5 = ORPHANHD
                    uint h = ent.archetypeName;
                    if (h == 0) continue;
                    if (excludeRx != null)
                    {
                        string en = JenkIndex.TryGetString(h);
                        if (!string.IsNullOrEmpty(en) && excludeRx.IsMatch(en.ToLowerInvariant()))
                        {
                            skippedExcluded++;
                            if (!excludedNames.Contains(en)) excludedNames.Add(en);
                            continue;
                        }
                    }
                    if (!hashes.Contains(h)) hashes.Add(h);
                }
            }
            int mloCount = ymap.CMloInstanceDefs != null ? ymap.CMloInstanceDefs.Length : 0;

            var models = new List<string>();
            var failed = new List<string>();
            for (int i = 0; i < hashes.Count; i++)
            {
                uint h = hashes[i];
                string an = JenkIndex.TryGetString(h);
                if (string.IsNullOrEmpty(an)) an = "hash_" + h.ToString("X8");
                Status("Modele " + (i + 1) + "/" + hashes.Count + " : " + an);
                string res;
                try { res = ExportHash(h, an, outDir); }
                catch (Exception ex) { res = Fail(ex.Message); }
                Match fm = Regex.Match(res, "\"file\":\"([^\"]+)\"");
                if (res.StartsWith("{\"ok\":true") && fm.Success) models.Add(fm.Groups[1].Value);
                else failed.Add(an);
            }

            return "{\"ok\":true,\"ymap\":" + Program.Js(ymapFile)
                + ",\"ymapdir\":" + Program.Js(ymapDir)
                + ",\"dir\":" + Program.Js(outDir)
                + ",\"entities\":" + entityCount
                + ",\"skippedlod\":" + skippedLod
                + ",\"excluded\":" + skippedExcluded
                + ",\"excludednames\":" + Program.JsList(excludedNames)
                + ",\"mlo\":" + mloCount
                + ",\"models\":" + Program.JsList(models)
                + ",\"failed\":" + Program.JsList(failed) + "}";
        }

        static void AddTxd(List<uint> txds, uint h)
        {
            if (h == 0) return;
            uint hd = HdHash(h);
            if (hd != h && !txds.Contains(hd)) txds.Add(hd);
            if (!txds.Contains(h)) txds.Add(h);
        }

        static string Fail(string msg)
        {
            return "{\"ok\":false,\"error\":" + Program.Js(msg) + "}";
        }
    }
}
