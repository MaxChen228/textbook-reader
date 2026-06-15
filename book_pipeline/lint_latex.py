#!/usr/bin/env python3
"""Lint LaTeX in chapter JSONs — find commands that KaTeX cannot render.

Usage: python3 lint_latex.py          # all chapters
       python3 lint_latex.py 9 11     # specific chapters
"""

import json, glob, os, sys, re

sys.path.insert(0, os.path.dirname(__file__))
from config import parse_ch_key, ch_dir_name

# KaTeX supported commands (comprehensive list)
KATEX_COMMANDS = {
    # Greek letters
    'alpha','beta','gamma','delta','epsilon','varepsilon','zeta','eta','theta',
    'vartheta','iota','kappa','lambda','mu','nu','xi','omicron','pi','varpi',
    'rho','varrho','sigma','varsigma','tau','upsilon','phi','varphi','chi',
    'psi','omega','Gamma','Delta','Theta','Lambda','Xi','Pi','Sigma','Upsilon',
    'Phi','Psi','Omega','digamma','varkappa',
    # Operators & math
    'frac','dfrac','tfrac','cfrac','binom','dbinom','tbinom',
    'sqrt','sum','prod','coprod','int','iint','iiint','oint','oiint',
    'partial','nabla','infty','forall','exists','nexists',
    'pm','mp','times','cdot','div','circ','ast','star','dagger','ddagger',
    'land','lor','lnot','neg','cup','cap','setminus','subset','supset',
    'subseteq','supseteq','subsetneq','supsetneq','in','notin','ni',
    'emptyset','varnothing',
    # Relations
    'eq','ne','neq','lt','gt','le','ge','leq','geq','ll','gg',
    'sim','simeq','approx','cong','equiv','propto','perp','parallel',
    'prec','succ','preceq','succeq','asymp','bowtie','models','vdash','dashv',
    # Arrows
    'leftarrow','rightarrow','Leftarrow','Rightarrow','leftrightarrow',
    'Leftrightarrow','longleftarrow','longrightarrow','Longleftarrow',
    'Longrightarrow','longleftrightarrow','Longleftrightarrow',
    'to','gets','mapsto','longmapsto','uparrow','downarrow','Uparrow','Downarrow',
    'updownarrow','Updownarrow','nearrow','searrow','swarrow','nwarrow',
    'hookrightarrow','hookleftarrow','rightharpoonup','rightharpoondown',
    'leftharpoonup','leftharpoondown','rightleftharpoons',
    'iff','implies','impliedby',
    'xleftarrow','xrightarrow','xmapsto',
    # Formatting
    'mathbf','mathrm','mathit','mathsf','mathtt','mathcal','mathscr','mathbb',
    'mathfrak','boldsymbol','bm','text','textbf','textit','textrm','textsf','texttt',
    'operatorname','operatorname*',
    'hat','bar','vec','dot','ddot','tilde','breve','acute','grave','check',
    'widetilde','widehat','widecheck','overline','underline',
    'overbrace','underbrace','overrightarrow','overleftarrow','overleftrightarrow',
    'overset','underset','stackrel','atop',
    'cancel','bcancel','xcancel','sout',
    # Delimiters
    'left','right','middle','big','Big','bigg','Bigg',
    'bigl','Bigl','biggl','Biggl','bigr','Bigr','biggr','Biggr',
    'bigm','Bigm','biggm','Biggm',
    'langle','rangle','lvert','rvert','lVert','rVert',
    'lfloor','rfloor','lceil','rceil','lgroup','rgroup',
    # Spacing
    'quad','qquad','enspace','thinspace','medspace','thickspace',
    'negthinspace','negmedspace','negthickspace',
    'hspace','kern','mkern','mskip','hskip','phantom','vphantom','hphantom',
    'smash','rlap','llap',
    # Environments
    'begin','end','tag','label','ref','eqref','nonumber','notag',
    'array','matrix','pmatrix','bmatrix','Bmatrix','vmatrix','Vmatrix',
    'cases','rcases','aligned','gathered','split','substack',
    # Sizing
    'displaystyle','textstyle','scriptstyle','scriptscriptstyle',
    'limits','nolimits',
    'tiny','scriptsize','footnotesize','small','normalsize',
    'large','Large','LARGE','huge','Huge',
    # Log-like
    'log','ln','exp','sin','cos','tan','cot','sec','csc',
    'arcsin','arccos','arctan','sinh','cosh','tanh','coth',
    'lim','limsup','liminf','max','min','sup','inf',
    'det','dim','ker','hom','arg','deg','gcd','Pr',
    # Misc
    'Re','Im','wp','ell','imath','jmath','hbar','AA',
    'prime','backprime','sharp','flat','natural',
    'ldots','cdots','vdots','ddots','dots','dotsb','dotsc','dotsi','dotsm',
    'angle','measuredangle','sphericalangle','triangle',
    'square','lozenge','checkmark','maltese',
    'clubsuit','diamondsuit','heartsuit','spadesuit',
    'triangleq','therefore','because',
    'Box','Diamond',
    'colorbox','color','textcolor','fcolorbox','boxed','fbox',
    'rule','space','nobreak','allowbreak','cr','newline',
    'bf','it','rm','sf','tt','cal','mit',
    'not','mod','bmod','pmod','pod',
    'above','over','choose',
    'htmlClass','htmlId','htmlStyle','htmlData',
    'href','url','textup','textmd',
    'medskip','bigskip','smallskip','par','indent','noindent',
    'centerdot','ltimes','rtimes','And',
}

def lint_chapter(base, ch_num):
    dn = ch_dir_name(ch_num)
    ch_dir = os.path.join(base, dn)
    ch_json = os.path.join(ch_dir, f'{dn}.json')
    if not os.path.exists(ch_json):
        return []

    with open(ch_json) as f:
        data = json.load(f)

    issues = []
    for bi, block in enumerate(data.get('blocks', [])):
        content = block.get('content', '')
        if not content:
            continue

        # Extract all LaTeX regions (inline $...$ and display $$...$$)
        for m in re.finditer(r'\$\$[\s\S]*?\$\$|\$[^$\n]+?\$', content):
            latex = m.group()
            # Find all commands
            for cm in re.finditer(r'\\([a-zA-Z]+)', latex):
                cmd = cm.group(1)
                if cmd not in KATEX_COMMANDS:
                    ctx_start = max(0, cm.start() - 15)
                    ctx_end = min(len(latex), cm.end() + 15)
                    issues.append({
                        'block': bi,
                        'type': block.get('type', ''),
                        'cmd': cmd,
                        'context': latex[ctx_start:ctx_end].replace('\n', ' '),
                    })

    return issues


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 lint_latex.py <book_dir> [chapter ...]")
        sys.exit(1)

    base = sys.argv[1]
    if len(sys.argv) > 2:
        chapters = [parse_ch_key(x) for x in sys.argv[2:]]
    else:
        chapters = []
        for d in sorted(glob.glob(os.path.join(base, 'ch*'))):
            raw = os.path.basename(d)[2:]
            ch = parse_ch_key(raw)
            dn = ch_dir_name(ch)
            if os.path.exists(os.path.join(d, f'{dn}.json')):
                chapters.append(ch)

    total = 0
    for ch in chapters:
        issues = lint_chapter(base, ch)
        dn = ch_dir_name(ch)
        if issues:
            from collections import Counter
            cmd_counts = Counter(i['cmd'] for i in issues)
            print(f"\n{dn}: {len(issues)} issue(s)")
            for cmd, count in cmd_counts.most_common():
                ex = next(i for i in issues if i['cmd'] == cmd)
                print(f"  \\{cmd} x{count}  ...{ex['context']}...")
            total += len(issues)
        else:
            print(f"{dn}: OK")

    print(f"\nTotal: {total} issues across {len(chapters)} chapters")


if __name__ == '__main__':
    main()
