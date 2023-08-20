import colorsys
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx
import tiktoken
from diskcache import Cache
from tqdm import tqdm

from aider import models

from .dump import dump  # noqa: F402


def to_tree(tags):
    if not tags:
        return ""

    tags = sorted(tags)

    output = ""
    last = [None] * len(tags[0])
    tab = "\t"
    for tag in tags:
        tag = list(tag)

        for i in range(len(last) + 1):
            if i == len(last):
                break
            if last[i] != tag[i]:
                break

        num_common = i

        indent = tab * num_common
        rest = tag[num_common:]
        for item in rest:
            output += indent + item + "\n"
            indent += tab
        last = tag

    return output


def fname_to_components(fname, with_colon):
    path_components = fname.split(os.sep)
    res = [pc + os.sep for pc in path_components[:-1]]
    if with_colon:
        res.append(path_components[-1] + ":")
    else:
        res.append(path_components[-1])
    return res


class RepoMap:
    CACHE_VERSION = 2
    TAGS_CACHE_DIR = f".aider.tags.cache.v{CACHE_VERSION}"

    cache_missing = False

    def __init__(
        self,
        map_tokens=1024,
        root=None,
        main_model=models.GPT4,
        io=None,
        repo_content_prefix=None,
        verbose=False,
    ):
        self.io = io
        self.verbose = verbose

        if not root:
            root = os.getcwd()
        self.root = root

        self.load_tags_cache()

        self.max_map_tokens = map_tokens

        self.tokenizer = tiktoken.encoding_for_model(main_model.name)
        self.repo_content_prefix = repo_content_prefix

    def get_repo_map(self, chat_files, other_files):
        if self.max_map_tokens <= 0:
            return

        if not other_files:
            return

        files_listing = self.get_ranked_tags_map(chat_files, other_files)
        if not files_listing:
            return

        num_tokens = self.token_count(files_listing)
        if self.verbose:
            self.io.tool_output(f"ctags map: {num_tokens/1024:.1f} k-tokens")

        if chat_files:
            other = "other "
        else:
            other = ""

        if self.repo_content_prefix:
            repo_content = self.repo_content_prefix.format(other=other)
        else:
            repo_content = ""

        repo_content += files_listing

        return repo_content

    def get_simple_files_map(self, other_files):
        fnames = []
        for fname in other_files:
            fname = self.get_rel_fname(fname)
            fname = fname_to_components(fname, False)
            fnames.append(fname)

        return to_tree(fnames)

    def token_count(self, string):
        return len(self.tokenizer.encode(string))

    def get_rel_fname(self, fname):
        return os.path.relpath(fname, self.root)

    def split_path(self, path):
        path = os.path.relpath(path, self.root)
        return [path + ":"]

    def load_tags_cache(self):
        path = Path(self.root) / self.TAGS_CACHE_DIR
        if not path.exists():
            self.cache_missing = True
        self.TAGS_CACHE = Cache(path)

    def save_tags_cache(self):
        pass

    def get_mtime(self, fname):
        try:
            return os.path.getmtime(fname)
        except FileNotFoundError:
            self.io.tool_error(f"File not found error: {fname}")

    def get_ranked_tags(self, chat_fnames, other_fnames):
        defines = defaultdict(set)
        references = defaultdict(list)
        definitions = defaultdict(set)

        personalization = dict()

        fnames = set(chat_fnames).union(set(other_fnames))
        chat_rel_fnames = set()

        fnames = sorted(fnames)

        if self.cache_missing:
            fnames = tqdm(fnames)
        self.cache_missing = False

        for fname in fnames:
            if not Path(fname).is_file():
                self.io.tool_error(f"Repo-map can't include {fname}")
                continue

            # dump(fname)
            rel_fname = os.path.relpath(fname, self.root)

            if fname in chat_fnames:
                personalization[rel_fname] = 1.0
                chat_rel_fnames.add(rel_fname)

            # TODO
            data = []

            for tag in data:
                ident = tag["name"]
                defines[ident].add(rel_fname)

                scope = tag.get("scope")
                kind = tag.get("kind")
                name = tag.get("name")
                signature = tag.get("signature")

                last = name
                if signature:
                    last += " " + signature

                res = [rel_fname]
                if scope:
                    res.append(scope)
                res += [kind, last]

                key = (rel_fname, ident)
                definitions[key].add(tuple(res))
                # definitions[key].add((rel_fname,))

            # TODO
            idents = []
            for ident in idents:
                # dump("ref", fname, ident)
                references[ident].append(rel_fname)

        idents = set(defines.keys()).intersection(set(references.keys()))

        G = nx.MultiDiGraph()

        for ident in idents:
            definers = defines[ident]
            for referencer, num_refs in Counter(references[ident]).items():
                for definer in definers:
                    if referencer == definer:
                        continue
                    G.add_edge(referencer, definer, weight=num_refs, ident=ident)

        if personalization:
            pers_args = dict(personalization=personalization, dangling=personalization)
        else:
            pers_args = dict()

        try:
            ranked = nx.pagerank(G, weight="weight", **pers_args)
        except ZeroDivisionError:
            return []

        # distribute the rank from each source node, across all of its out edges
        ranked_definitions = defaultdict(float)
        for src in G.nodes:
            src_rank = ranked[src]
            total_weight = sum(data["weight"] for _src, _dst, data in G.out_edges(src, data=True))
            # dump(src, src_rank, total_weight)
            for _src, dst, data in G.out_edges(src, data=True):
                data["rank"] = src_rank * data["weight"] / total_weight
                ident = data["ident"]
                ranked_definitions[(dst, ident)] += data["rank"]

        ranked_tags = []
        ranked_definitions = sorted(ranked_definitions.items(), reverse=True, key=lambda x: x[1])
        for (fname, ident), rank in ranked_definitions:
            # print(f"{rank:.03f} {fname} {ident}")
            if fname in chat_rel_fnames:
                continue
            ranked_tags += list(definitions.get((fname, ident), []))

        rel_other_fnames_without_tags = set(
            os.path.relpath(fname, self.root) for fname in other_fnames
        )

        fnames_already_included = set(rt[0] for rt in ranked_tags)

        top_rank = sorted([(rank, node) for (node, rank) in ranked.items()], reverse=True)
        for rank, fname in top_rank:
            if fname in rel_other_fnames_without_tags:
                rel_other_fnames_without_tags.remove(fname)
            if fname not in fnames_already_included:
                ranked_tags.append((fname,))

        for fname in rel_other_fnames_without_tags:
            ranked_tags.append((fname,))

        return ranked_tags

    def get_ranked_tags_map(self, chat_fnames, other_fnames=None):
        if not other_fnames:
            other_fnames = list()

        ranked_tags = self.get_ranked_tags(chat_fnames, other_fnames)
        num_tags = len(ranked_tags)

        lower_bound = 0
        upper_bound = num_tags
        best_tree = None

        while lower_bound <= upper_bound:
            middle = (lower_bound + upper_bound) // 2
            tree = to_tree(ranked_tags[:middle])
            num_tokens = self.token_count(tree)
            # dump(middle, num_tokens)

            if num_tokens < self.max_map_tokens:
                best_tree = tree
                lower_bound = middle + 1
            else:
                upper_bound = middle - 1

        return best_tree


def find_py_files(directory):
    if not os.path.isdir(directory):
        return [directory]

    py_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))
    return py_files


def get_random_color():
    hue = random.random()
    r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(hue, 1, 0.75)]
    res = f"#{r:02x}{g:02x}{b:02x}"
    return res


if __name__ == "__main__":
    fnames = sys.argv[1:]

    chat_fnames = []
    other_fnames = []
    for dname in sys.argv[1:]:
        if ".venv" in dname:
            other_fnames += find_py_files(dname)
        else:
            chat_fnames += find_py_files(dname)

    root = os.path.commonpath(chat_fnames)

    rm = RepoMap(root=root)
    repo_map = rm.get_ranked_tags_map(chat_fnames, other_fnames)

    dump(len(repo_map))
    print(repo_map)
