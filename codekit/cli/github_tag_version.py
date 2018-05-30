#!/usr/bin/env python3
"""
Use URL to EUPS candidate tag file to git tag repos with official version
"""

# Technical Debt
# --------------
# - support repos.yaml for github repo resolution
# - worth doing the smart thing for externals? (yes for Sims)
# - deal with authentication version

# Known Bugs
# ----------
# Yeah, the candidate logic is broken, will fix


from codekit.codetools import debug, info, warn, error
from codekit import codetools, eups, pygithub, versiondb
import argparse
import github
import itertools
import os
import re
import sys
import textwrap


class GitTagExistsError(Exception):
    pass


def parse_args():
    """Parse command-line arguments"""

    parser = argparse.ArgumentParser(
        prog='github-tag-version',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""

        Tag all repositories in a GitHub org using a team-based scheme

        Examples:
        github-tag-version \\
            --org lsst \\
            --allow-team 'Data Management' \\
            --allow-team 'DM Externals' \\
            'w.2018.18' 'b3595'

        github-tag-version \\
            --org lsst \\
            --allow-team 'Data Management' \\
            --allow-team 'DM Externals' \\
            --external-team 'DM Externals' \\
            --candidate v11_0_rc2 \\
            11.0.rc2 b1679

        Note that the access token must have access to these oauth scopes:
            * read:org
            * repo

        The token generated by `github-auth --user` should have sufficient
        permissions.
        """),
        epilog='Part of codekit: https://github.com/lsst-sqre/sqre-codekit'
    )

    parser.add_argument('tag')
    parser.add_argument('manifest')
    parser.add_argument(
        '--org',
        required=True,
        help="Github organization")
    parser.add_argument(
        '--allow-team',
        action='append',
        required=True,
        help='git repos to be tagged MUST be a member of ONE or more of'
             ' these teams (can specify several times)')
    parser.add_argument(
        '--external-team',
        action='append',
        help='git repos in this team MUST not have tags that start with a'
             ' number. Any requested tag that violates this policy will be'
             ' prefixed with \'v\' (can specify several times)')
    parser.add_argument(
        '--deny-team',
        action='append',
        help='git repos to be tagged MUST NOT be a member of ANY of'
             ' these teams (can specify several times)')
    parser.add_argument('--candidate')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument(
        '--user',
        help='Name of person making the tag - defaults to gitconfig value')
    parser.add_argument(
        '--email',
        help='Email address of tagger - defaults to gitconfig value')
    parser.add_argument(
        '--token-path',
        default='~/.sq_github_token_delete',
        help='Use a token (made with github-auth) in a non-standard location')
    parser.add_argument(
        '--token',
        default=None,
        help='Literal github personal access token string')
    parser.add_argument(
        '--versiondb-base-url',
        default=os.getenv('LSST_VERSIONDB_BASE_URL'),
        help='Override the default versiondb base url')
    parser.add_argument(
        '--eupstag-base-url',
        help='Override the default eupstag base url')
    parser.add_argument(
        '--force-tag',
        action='store_true',
        help='Force moving pre-existing annotated git tags.')
    parser.add_argument(
        '--ignore-version',
        action='store_true',
        help='Ignore version strings'
             ' when cross referencing eups tag and manifest data.')
    parser.add_argument(
        '--limit',
        default=None,
        type=int,
        help='Maximum number of products/repos to tags. (useful for testing)')
    parser.add_argument(
        '--fail-fast',
        action='store_true',
        help='Fail immediately on github API errors.')
    parser.add_argument(
        '--no-fail-fast',
        action='store_const',
        const=False,
        dest='fail_fast',
        help='DO NOT Fail immediately on github API errors. (default)')
    parser.add_argument(
        '-d', '--debug',
        action='count',
        default=os.getenv('DM_SQUARE_DEBUG'),
        help='Debug mode (can specify several times)')
    parser.add_argument('-v', '--version', action=codetools.ScmVersionAction)
    return parser.parse_args()


def cmp_dict(d1, d2, ignore_keys=[]):
    """Compare dicts ignoring select keys"""
    # https://stackoverflow.com/questions/10480806/compare-dictionaries-ignoring-specific-keys
    return {k: v for k, v in d1.items() if k not in ignore_keys} \
        == {k: v for k, v in d2.items() if k not in ignore_keys}


def cross_reference_products(
    eups_products,
    manifest_products,
    ignore_version=False,
    fail_fast=False,
):
    """
    Cross reference EupsTag and Manifest data and return a merged result

    Parameters
    ----------
    eups_products:
    manifest:
    fail_fast: bool
    ignore_versions: bool

    Returns
    -------
    products: dict

    Raises
    ------
    RuntimeError
        Upon error if `fail_fast` is `True`.
    """
    products = {}

    problems = []
    for name, eups_data in eups_products.items():
        try:
            manifest_data = manifest_products[name]
        except KeyError:
            yikes = RuntimeError(textwrap.dedent("""\
                failed to find record in manifest for:
                  {product} {eups_version}\
                """).format(
                product=name,
                eups_version=eups_data['eups_version'],
            ))
            if fail_fast:
                raise yikes from None
            problems.append(yikes)
            error(yikes)

        if ignore_version:
            # ignore the manifest eups_version string by simply setting it to
            # the eups tag value.  This ensures that the eups tag value will be
            # passed though.
            manifest_data = manifest_data.copy()
            manifest_data['eups_version'] = eups_data['eups_version']

        if eups_data['eups_version'] != manifest_data['eups_version']:
            yikes = RuntimeError(textwrap.dedent("""\
                eups version string mismatch:
                  eups tag: {product} {eups_eups_version}
                  manifest: {product} {manifest_eups_version}\
                """).format(
                product=name,
                eups_eups_version=eups_data['eups_version'],
                manifest_eups_version=manifest_data['eups_version'],
            ))
            if fail_fast:
                raise yikes
            problems.append(yikes)
            error(yikes)

        products[name] = eups_data.copy()
        products[name].update(manifest_data)

    if problems:
        msg = "{n} product(s) have errors".format(n=len(problems))
        raise codetools.DogpileError(problems, msg)

    return products


def get_repo_for_products(
    org,
    products,
    allow_teams,
    ext_teams,
    deny_teams,
    fail_fast=False
):
    debug("allowed teams: {allow}".format(allow=allow_teams))
    debug("external teams: {ext}".format(ext=ext_teams))
    debug("denied teams: {deny}".format(deny=deny_teams))

    resolved_products = {}

    problems = []
    for name, data in products.items():
        debug("looking for git repo for: {name} [{ver}]".format(
            name=name,
            ver=data['eups_version']
        ))

        try:
            repo = org.get_repo(name)
        except github.UnknownObjectException as e:
            yikes = pygithub.CaughtUnknownObjectError(name, e)
            if fail_fast:
                raise yikes from None
            problems.append(yikes)
            error(yikes)

            continue

        debug("  found: {slug}".format(slug=repo.full_name))

        repo_team_names = [t.name for t in repo.get_teams()]
        debug("  teams: {teams}".format(teams=repo_team_names))

        try:
            pygithub.check_repo_teams(
                repo,
                allow_teams=allow_teams,
                deny_teams=deny_teams,
                team_names=repo_team_names
            )
        except pygithub.RepositoryTeamMembershipError as e:
            if fail_fast:
                raise
            problems.append(e)
            error(e)

            continue

        has_ext_team = any(x in repo_team_names for x in ext_teams)
        debug("  external repo: {v}".format(v=has_ext_team))

        resolved_products[name] = data.copy()
        resolved_products[name]['repo'] = repo
        resolved_products[name]['v'] = has_ext_team

    if problems:
        msg = "{n} product(s) have errors".format(n=len(problems))
        raise codetools.DogpileError(problems, msg)

    return resolved_products


def author_to_dict(obj):
    """Who needs a switch/case statement when you can instead use this easy to
    comprehend drivel?
    """
    def default():
        raise RuntimeError("unsupported type {t}".format(t=type(obj).__name__))

    # a more pythonic way to handle this would be several try blocks to catch
    # missing attributes
    return {
        # GitAuthor has name,email,date properties
        'GitAuthor': lambda x: {'name': x.name, 'email': x.email},
        # InputGitAuthor only has _identity, which returns a dict
        'InputGitAuthor': lambda x: x._identity,
    }.get(type(obj).__name__, lambda x: default())(obj)


def cmp_gitauthor(a, b):
    # ignore date
    if cmp_dict(author_to_dict(a), author_to_dict(b), ['date']):
        return True

    return False


def cmp_existing_git_tag(t_tag, e_tag):
    assert isinstance(t_tag, dict)
    assert isinstance(e_tag, github.GitTag.GitTag)

    # ignore date when comparing tag objects
    if t_tag['sha'] == e_tag.object.sha and \
       t_tag['message'] == e_tag.message and \
       cmp_gitauthor(t_tag['tagger'], e_tag.tagger):
        return True

    return False


def check_existing_git_tag(repo, t_tag):
    """
    Check for a pre-existng tag in the github repo.

    Parameters
    ----------
    repo : github.Repository.Repository
        repo to inspect for an existing tagsdf
    t_tag: dict
        dict repesenting a target git tag

    Returns
    -------
    insync : `bool`
        True if tag exists and is in sync. False if tag does not exist.

    Raises
    ------
    GitTagExistsError
        If tag exists but is not in sync.
    """

    assert isinstance(repo, github.Repository.Repository), type(repo)
    assert isinstance(t_tag, dict)

    debug("looking for existing tag: {tag}"
          .format(tag=t_tag['name']))

    # find ref/tag by name
    e_ref = pygithub.find_tag_by_name(repo, t_tag['name'])
    if not e_ref:
        debug("  not found: {tag}".format(tag=t_tag['name']))
        return False

    # find tag object pointed to by the ref
    e_tag = repo.get_git_tag(e_ref.object.sha)
    debug("  found existing tag: {tag} [sha]".format(
        tag=e_tag.tag,
        sha=e_tag.sha
    ))

    if cmp_existing_git_tag(t_tag, e_tag):
        return True

    warn(textwrap.dedent("""\
        tag {tag} already exists with conflicting values:
          existing:
            sha: {e_sha}
            message: {e_message}
            tagger: {e_tagger}
          target:
            sha: {t_sha}
            message: {t_message}
            tagger: {t_tagger}\
    """).format(
        tag=t_tag['name'],
        e_sha=e_tag.sha,
        e_message=e_tag.message,
        e_tagger=e_tag.tagger,
        t_sha=t_tag['sha'],
        t_message=t_tag['message'],
        t_tagger=t_tag['tagger'],
    ))

    raise GitTagExistsError("tag already exists: {tag} [{sha}]"
                            .format(tag=e_tag.tag, sha=e_tag.sha))


def check_product_tags(
    products,
    tag_template,
    force_tag=False,
    fail_fast=False,
):
    checked_products = {}

    problems = []
    for name, data in products.items():
        # "target tag"
        t_tag = tag_template.copy()
        t_tag['sha'] = data['sha']

        # prefix tag name with `v`?
        if data['v'] and re.match('\d', t_tag['name']):
            t_tag['name'] = 'v' + t_tag['name']

        # control whether to create a new tag or update an existing one
        update_tag = False

        try:
            # if the existing tag is in sync, do nothing
            if check_existing_git_tag(data['repo'], t_tag):
                warn(textwrap.dedent("""\
                    No action for {repo}
                      existing tag: {tag} is already in sync\
                    """).format(
                    repo=data['repo'].full_name,
                    tag=t_tag['name'],
                ))

                continue
        except github.RateLimitExceededException:
            raise
        except GitTagExistsError as e:
            # if force_tag is set, and the tag already exists, set
            # update_tag and fall through. Otherwise, treat it as any other
            # exception.
            if force_tag:
                warn(textwrap.dedent("""\
                      existing tag: {tag} WILL BE MOVED\
                    """).format(
                    repo=data['repo'].full_name,
                    tag=t_tag['name'],
                ))
            elif fail_fast:
                raise
            else:
                problems.append(e)
                error(e)
                continue
        except github.GithubException as e:
            yikes = pygithub.CaughtRepositoryError(data['repo'], e)

            if fail_fast:
                raise yikes from None
            else:
                problems.append(yikes)
                error(yikes)
                continue

        checked_products[name] = data.copy()
        checked_products[name]['target_tag'] = t_tag
        checked_products[name]['update_tag'] = update_tag

    if problems:
        msg = "{n} product(s) have errors".format(n=len(problems))
        raise codetools.DogpileError(problems, msg)

    return checked_products


def tag_products(
    products,
    fail_fast=False,
    dry_run=False,
):
    problems = []
    for name, data in products.items():
        t_tag = data['target_tag']

        info(textwrap.dedent("""\
            tagging repo: {repo} @
              sha: {sha} as {gt}
              (eups version: {et})
              external repo: {v}
              replace existing tag: {update}\
            """).format(
            repo=data['repo'].full_name,
            sha=t_tag['sha'],
            gt=t_tag['name'],
            et=data['eups_version'],
            v=data['v'],
            update=data['update_tag'],
        ))

        if dry_run:
            info('  (noop)')
            continue

        try:
            tag_obj = data['repo'].create_git_tag(
                t_tag['name'],
                t_tag['message'],
                t_tag['sha'],
                'commit',
                tagger=t_tag['tagger'],
            )
            debug("  created tag object {tag_obj}".format(tag_obj=tag_obj))

            if data['update_tag']:
                ref = pygithub.find_tag_by_name(
                    data['repo'],
                    t_tag['name'],
                    safe=False,
                )
                ref.edit(tag_obj.sha, force=True)
                debug("  updated existing ref: {ref}".format(ref=ref))
            else:
                ref = data['repo'].create_git_ref(
                    "refs/tags/{t}".format(t=t_tag['name']),
                    tag_obj.sha
                )
                debug("  created ref: {ref}".format(ref=ref))
        except github.RateLimitExceededException:
            raise
        except github.GithubException as e:
            yikes = pygithub.CaughtRepositoryError(data['repo'], e)
            if fail_fast:
                raise yikes from None
            problems.append(yikes)
            error(yikes)

    if problems:
        msg = "{n} tag failures".format(n=len(problems))
        raise codetools.DogpileError(problems, msg)


def run():
    """Create the tag"""

    args = parse_args()

    codetools.setup_logging(args.debug)

    version = args.tag

    # if email not specified, try getting it from the gitconfig
    git_email = codetools.lookup_email(args)
    # ditto for the name of the git user
    git_user = codetools.lookup_user(args)

    # The candidate is assumed to be the requested EUPS tag unless
    # otherwise specified with the --candidate option The reason to
    # currently do this is that for weeklies and other internal builds,
    # it's okay to eups publish the weekly and git tag post-facto. However
    # for official releases, we don't want to publish until the git tag
    # goes down, because we want to eups publish the build that has the
    # official versions in the eups ref.
    candidate = args.candidate if args.candidate else args.tag

    manifest = args.manifest  # sadly we need to "just" know this
    message_template = 'Version {v} release from {c}/{b}'
    message = message_template.format(v=version, c=candidate, b=manifest)

    tagger = github.InputGitAuthor(
        git_user,
        git_email,
        codetools.current_timestamp(),
    )
    debug(tagger)

    # all tags should be the same across repos -- just add the 'sha' key and
    # stir
    tag_template = {
        'name': version,
        'message': message,
        'tagger': tagger,
    }

    debug(tag_template)

    global g
    g = pygithub.login_github(token_path=args.token_path, token=args.token)
    org = g.get_organization(args.org)
    debug("tagging repos in org: {org}".format(org=org.login))

    # generate eups-style version
    # eups no likey semantic versioning markup, wants underscores
    cmap = str.maketrans('.-', '__')
    eups_candidate = candidate.translate(cmap)

    eups_products = eups.EupsTag(
        eups_candidate,
        base_url=args.eupstag_base_url).products
    manifest_products = versiondb.Manifest(
        manifest,
        base_url=args.versiondb_base_url).products

    # do not fail-fast on non-write operations
    products = cross_reference_products(
        eups_products,
        manifest_products,
        ignore_version=args.ignore_version,
        fail_fast=False,
    )

    if args.limit:
        products = dict(itertools.islice(products.items(), args.limit))

    # do not fail-fast on non-write operations
    products = get_repo_for_products(
        org=org,
        products=products,
        allow_teams=args.allow_team,
        ext_teams=args.external_team,
        deny_teams=args.deny_team,
        fail_fast=False,
    )

    # do not fail-fast on non-write operations
    products_to_tag = check_product_tags(
        products,
        tag_template,
        force_tag=args.force_tag,
        fail_fast=False,
    )

    tag_products(
        products_to_tag,
        fail_fast=args.fail_fast,
        dry_run=args.dry_run,
    )


def main():
    try:
        run()
    except codetools.DogpileError as e:
        error(e)
        n = len(e.errors)
        sys.exit(n if n < 256 else 255)
    finally:
        if 'g' in globals():
            pygithub.debug_ratelimit(g)


if __name__ == '__main__':
    main()
