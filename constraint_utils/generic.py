from gnomad_hail import *
from gnomad_hail.utils.plotting import *


def reverse_complement_bases(bases: hl.expr.StringExpression) -> hl.expr.StringExpression:
    return hl.delimit(hl.range(bases.length() - 1, -1, -1).map(lambda i: flip_base(bases[i])), '')
    # return bases[::-1].map(lambda x: flip_base(x))


def flip_base(base: hl.expr.StringExpression) -> hl.expr.StringExpression:
    return (hl.switch(base)
            .when('A', 'T')
            .when('T', 'A')
            .when('G', 'C')
            .when('C', 'G')
            .default(base))


def collapse_strand(ht: Union[hl.Table, hl.MatrixTable]) -> Union[hl.Table, hl.MatrixTable]:
    collapse_expr = {
        'ref': hl.cond(((ht.ref == 'G') | (ht.ref == 'T')),
                       reverse_complement_bases(ht.ref), ht.ref),
        'alt': hl.cond(((ht.ref == 'G') | (ht.ref == 'T')),
                       reverse_complement_bases(ht.alt), ht.alt),
        'context': hl.cond(((ht.ref == 'G') | (ht.ref == 'T')),
                           reverse_complement_bases(ht.context), ht.context),
        'was_flipped': (ht.ref == 'G') | (ht.ref == 'T')
    }
    return ht.annotate(**collapse_expr) if isinstance(ht, hl.Table) else ht.annotate_rows(**collapse_expr)


def downsampling_counts_expr(ht: Union[hl.Table, hl.MatrixTable], pop: str = 'global', variant_quality: str = 'adj',
                             singleton: bool = False) -> hl.expr.ArrayExpression:
    return hl.agg.array_sum(
        hl.map(lambda f: hl.int(f.AC[1] == 1) if singleton else hl.int(f.AC[1] > 0), hl.sorted(
            hl.filter(
                lambda f: (f.meta.size() == 3) & (f.meta.get('group') == variant_quality) &
                          (f.meta.get('pop') == pop) & f.meta.contains('downsampling'),
                ht.freq),
            key=lambda f: hl.int(f.meta['downsampling'])
        )))


def count_variants(ht: hl.Table,
                   count_singletons: bool = False, count_downsamplings: Optional[List[str]] = (),
                   additional_grouping: Optional[List[str]] = (), partition_hint: int = 100,
                   omit_methylation: bool = False, return_type_only: bool = False,
                   force_grouping: bool = False, singleton_expression: hl.expr.BooleanExpression = None) -> Union[hl.Table, Any]:
    """
    Count variants by context, ref, alt, methylation_level
    """

    grouping = hl.struct(context=ht.context, ref=ht.ref, alt=ht.alt)
    if not omit_methylation:
        grouping = grouping.annotate(methylation_level=ht.methylation_level)
    for group in additional_grouping:
        grouping = grouping.annotate(**{group: ht[group]})

    if count_singletons:
        # singleton = hl.any(lambda f: (f.meta.size() == 1) & (f.meta.get('group') == 'adj') & (f.AC[1] == 1), ht.freq)
        if singleton_expression is None:
            singleton_expression = ht.freq[0].AC == 1

    if count_downsamplings or force_grouping:
        # Slower, but more flexible (allows for downsampling agg's)
        output = {'variant_count': hl.agg.count()}
        for pop in count_downsamplings:
            output[f'downsampling_counts_{pop}'] = downsampling_counts_expr(ht, pop)
        if count_singletons:
            output['singleton_count'] = hl.agg.count_where(singleton_expression)
            for pop in count_downsamplings:
                output[f'singleton_downsampling_counts_{pop}'] = downsampling_counts_expr(ht, pop, singleton=True)
        return ht.group_by(**grouping)._set_buffer_size(1000).partition_hint(partition_hint).aggregate(**output)
    else:
        agg = {'variant_count': hl.agg.counter(grouping)}
        if count_singletons:
            agg['singleton_count'] = hl.agg.counter(hl.agg.filter(singleton_expression, grouping))

        if return_type_only:
            return agg['variant_count'].dtype
        else:
            return ht.aggregate(hl.struct(**agg))


def annotate_variant_types(t: Union[hl.MatrixTable, hl.Table],
                           heptamers: bool = False) -> Union[hl.MatrixTable, hl.Table]:
    """
    Adds cpg, transition, and variant_type, variant_type_model columns
    """
    mid_index = 3 if heptamers else 1
    transition_expr = (((t.ref == "A") & (t.alt == "G")) | ((t.ref == "G") & (t.alt == "A")) |
                       ((t.ref == "T") & (t.alt == "C")) | ((t.ref == "C") & (t.alt == "T")))
    cpg_expr = (((t.ref == "G") & (t.alt == "A") & (t.context[mid_index - 1:mid_index] == 'C')) |
                ((t.ref == "C") & (t.alt == "T") & (t.context[mid_index + 1:mid_index + 2] == 'G')))
    if isinstance(t, hl.MatrixTable):
        t = t.annotate_rows(transition=transition_expr, cpg=cpg_expr)
    else:
        t = t.annotate(transition=transition_expr, cpg=cpg_expr)
    variant_type_expr = (hl.case()
                         .when(t.cpg, 'CpG')
                         .when(t.transition, 'non-CpG transition')
                         .default('transversion'))
    variant_type_model_expr = hl.cond(t.cpg, t.context, "non-CpG")
    if isinstance(t, hl.MatrixTable):
        return t.annotate_rows(variant_type=variant_type_expr, variant_type_model=variant_type_model_expr)
    else:
        return t.annotate(variant_type=variant_type_expr, variant_type_model=variant_type_model_expr)


def round_fields(ht: hl.Table, field: str, bin_size: int) -> hl.Table:
    """
    Round field in table to bin_size
    """
    return ht.annotate(**{field: hl.int(ht[field] / bin_size) * bin_size})


def set_kt_cols_to_zero(ht: hl.Table, fields: List[hl.expr.NumericExpression]) -> hl.Table:
    """
    Sets values of fields in ht to zero if missing
    """
    return ht.annotate(**{field: hl.or_else(field, 0) for field in fields})


def rebin_methylation(t: Union[hl.MatrixTable, hl.Table], bins: int=20) -> Union[hl.MatrixTable, hl.Table]:
    """
    Rebins methylation.level based on methylation.value to `bins` (assumes bi-allelic)
    """
    methylation_expr = t.methylation.annotate(level=hl.or_missing(
        hl.is_transition(hl.alleles[0], hl.alleles[1]),
        hl.range(bins - 1, -1, -1).find(lambda e: t.methylation.value * bins >= e)))
    if isinstance(t, hl.MatrixTable):
        return t.annotate_rows(methylation=methylation_expr)
    else:
        return t.annotate(methylation=methylation_expr)


def trimer_from_heptamer(t: Union[hl.MatrixTable, hl.Table]) -> Union[hl.MatrixTable, hl.Table]:
    trimer_expr = hl.cond(hl.len(t.context) == 7, t.context[2:5], t.context)
    return t.annotate_rows(context=trimer_expr) if isinstance(t, hl.MatrixTable) else t.annotate(context=trimer_expr)


def filter_for_mu(t: Union[hl.MatrixTable, hl.Table]) -> Union[hl.MatrixTable, hl.Table]:
    """
    Filter to non-coding annotations, remove GERP outliers
    GERP cutoffs determined by finding 5% and 95% percentiles on list generated by:
    ```
    gerp_data = ht.aggregate(gerp=hl.agg.hist(context_ht.gerp, -12.3, 6.17, 100))
    cumulative_data = np.cumsum(summary_hist.gerp.bin_freq) + summary_hist.gerp.n_smaller
    np.append(cumulative_data, [cumulative_data[-1] + summary_hist.gerp.n_larger])
    list(zip(summary_hist.gerp.bin_edges, cumulative_data / max(cumulative_data)))
    ```
    """
    # This would pull out the consequence specific to each alternate allele, but in the case of intron and intergenic,
    # the worst consequence for one substitution is also the worst for any other at the same site, so it's ok
    # t = process_consequences(t)
    # intronic = (t.vep.worst_consequence_term == "intron_variant")
    # intergenic = (hl.is_missing(t.vep.transcript_consequences) | (hl.len(t.vep.transcript_consequences) == 0)
    #               ) & (hl.is_defined(t.vep.intergenic_consequences) | (hl.len(t.vep.intergenic_consequences) > 0))
    # criteria = (intronic | intergenic) & (t.gerp > -3.9885) & (t.gerp < 2.6607)
    criteria = ((t.gerp > -3.9885) & (t.gerp < 2.6607) &
                ((t.vep.most_severe_consequence == 'intron_variant') |
                 (t.vep.most_severe_consequence == 'intergenic_variant')))
    return t.filter_rows(criteria) if isinstance(t, hl.MatrixTable) else t.filter(criteria)


def filter_to_pass(t: Union[hl.MatrixTable, hl.Table]) -> Union[hl.MatrixTable, hl.Table]:
    criteria = hl.all(lambda x: ~x, list(t.filters.values()))
    return t.filter_rows(criteria) if isinstance(t, hl.MatrixTable) else t.filter(criteria)


def filter_vep(t: Union[hl.MatrixTable, hl.Table],
               canonical: bool = True, synonymous: bool = True) -> Union[hl.MatrixTable, hl.Table]:
    if synonymous: t = filter_vep_to_synonymous_variants(t)
    if canonical: t = filter_vep_to_canonical_transcripts(t)
    criteria = hl.is_defined(t.vep.transcript_consequences)
    return t.filter_rows(criteria) if isinstance(t, hl.MatrixTable) else t.filter(criteria)


def fast_filter_vep(t: Union[hl.Table, hl.MatrixTable], vep_root: str = 'vep', syn: bool = True, canonical: bool = True,
                    filter_empty: bool = True) -> Union[hl.Table, hl.MatrixTable]:
    transcript_csqs = t[vep_root].transcript_consequences
    criteria = [lambda csq: True]
    if syn: criteria.append(lambda csq: csq.most_severe_consequence == "synonymous_variant")
    if canonical: criteria.append(lambda csq: csq.canonical == 1)

    def combine_functions(func_list, x):
        cond = func_list[0](x)
        for c in func_list[1:]:
            cond &= c(x)
        return cond
    transcript_csqs = transcript_csqs.filter(lambda x: combine_functions(criteria, x))
    vep_data = t[vep_root].annotate(transcript_consequences=transcript_csqs)
    t = t.annotate_rows(**{vep_root: vep_data}) if isinstance(t, hl.MatrixTable) else t.annotate(**{vep_root: vep_data})
    if not filter_empty:
        return t
    criteria = hl.is_defined(t.vep.transcript_consequences) & (hl.len(t.vep.transcript_consequences) > 0)
    return t.filter_rows(criteria) if isinstance(t, hl.MatrixTable) else t.filter(criteria)


def remove_coverage_outliers(t: Union[hl.MatrixTable, hl.Table]) -> Union[hl.MatrixTable, hl.Table]:
    """
    Keep only loci where genome coverage was between 15 and 60
    """
    criteria = (t.coverage.genomes.mean >= 15) & (t.coverage.genomes.mean <= 60)
    return t.filter_rows(criteria) if isinstance(t, hl.MatrixTable) else t.filter(criteria)


# Misc
def maps_old_model(ht: hl.Table, grouping: List[str] = ()) -> hl.Table:
    ht = count_variants(ht, count_singletons=True, additional_grouping=('worst_csq', *grouping), force_grouping=True, omit_methylation=True)
    from .constraint_basics import get_old_mu_data
    mutation_ht = get_old_mu_data()
    ht = ht.annotate(mu=mutation_ht[hl.struct(context=ht.context, ref=ht.ref, alt=ht.alt)].mu_snp,
                     ps=ht.singleton_count / ht.variant_count)
    syn_ps_ht = ht.filter(ht.worst_csq == 'synonymous_variant')
    syn_ps_ht = syn_ps_ht.group_by(syn_ps_ht.mu).aggregate(singleton_count=hl.agg.sum(syn_ps_ht.singleton_count),
                                                           variant_count=hl.agg.sum(syn_ps_ht.variant_count))
    syn_ps_ht = syn_ps_ht.annotate(ps=syn_ps_ht.singleton_count / syn_ps_ht.variant_count)
    assert syn_ps_ht.all(hl.is_defined(syn_ps_ht.mu))

    lm = syn_ps_ht.aggregate(hl.agg.linreg(syn_ps_ht.ps, [1, syn_ps_ht.mu],
                                           weight=syn_ps_ht.variant_count).beta)
    ht = ht.annotate(expected_singletons=(ht.mu * lm[1] + lm[0]) * ht.variant_count)

    agg_ht = (ht.group_by('worst_csq', *grouping)
              .aggregate(singleton_count=hl.agg.sum(ht.singleton_count),
                         expected_singletons=hl.agg.sum(ht.expected_singletons),
                         variant_count=hl.agg.sum(ht.variant_count)))
    agg_ht = agg_ht.annotate(ps=agg_ht.singleton_count / agg_ht.variant_count,
                             maps=(agg_ht.singleton_count - agg_ht.expected_singletons) / agg_ht.variant_count)
    agg_ht = agg_ht.annotate(sem_ps=(agg_ht.ps * (1 - agg_ht.ps) / agg_ht.variant_count) ** 0.5)
    return agg_ht


def maps(ht: hl.Table, mutation_ht: hl.Table, additional_grouping: List[str] = [],
         singleton_expression: hl.expr.BooleanExpression = None, skip_worst_csq: bool = False) -> hl.Table:
    if not skip_worst_csq: additional_grouping.insert(0, 'worst_csq')
    ht = count_variants(ht, count_singletons=True, additional_grouping=additional_grouping,
                        force_grouping=True, singleton_expression=singleton_expression)
    ht = ht.annotate(mu=mutation_ht[
        hl.struct(context=ht.context, ref=ht.ref, alt=ht.alt, methylation_level=ht.methylation_level)].mu_snp,
                     ps=ht.singleton_count / ht.variant_count)
    syn_ps_ht = ht.filter(ht.worst_csq == 'synonymous_variant')
    syn_ps_ht = syn_ps_ht.group_by(syn_ps_ht.mu).aggregate(singleton_count=hl.agg.sum(syn_ps_ht.singleton_count),
                                                           variant_count=hl.agg.sum(syn_ps_ht.variant_count))
    syn_ps_ht = syn_ps_ht.annotate(ps=syn_ps_ht.singleton_count / syn_ps_ht.variant_count)
    if not syn_ps_ht.all(hl.is_defined(syn_ps_ht.mu)):
        print('Some mu were not found...')
        print(syn_ps_ht.aggregate(hl.agg.filter(hl.is_missing(syn_ps_ht.mu), hl.agg.take(syn_ps_ht.row, 1)[0])))
        sys.exit(1)

    lm = syn_ps_ht.aggregate(hl.agg.linreg(syn_ps_ht.ps, [1, syn_ps_ht.mu],
                                           weight=syn_ps_ht.variant_count).beta)
    print(f'Got MAPS calibration model of: slope: {lm[1]}, intercept: {lm[0]}')
    ht = ht.annotate(expected_singletons=(ht.mu * lm[1] + lm[0]) * ht.variant_count)

    agg_ht = (ht.group_by(*additional_grouping)
              .aggregate(singleton_count=hl.agg.sum(ht.singleton_count),
                         expected_singletons=hl.agg.sum(ht.expected_singletons),
                         variant_count=hl.agg.sum(ht.variant_count)))
    agg_ht = agg_ht.annotate(ps=agg_ht.singleton_count / agg_ht.variant_count,
                             maps=(agg_ht.singleton_count - agg_ht.expected_singletons) / agg_ht.variant_count)
    agg_ht = agg_ht.annotate(maps_sem=(agg_ht.ps * (1 - agg_ht.ps) / agg_ht.variant_count) ** 0.5)
    return agg_ht


def maps_two_model(ht: hl.Table, mutation_ht: hl.Table, grouping: List[str] = ()) -> hl.Table:
    trimer = False
    ht = count_variants(ht, count_singletons=True, additional_grouping=('worst_csq', *grouping), force_grouping=True)
    ht = annotate_variant_types(ht.annotate(mu=mutation_ht[
        hl.struct(context=ht.context, ref=ht.ref, alt=ht.alt, methylation_level=ht.methylation_level)].mu_snp,
                     ps=ht.singleton_count / ht.variant_count), trimer)
    syn_ps_ht = ht.filter(ht.worst_csq == 'synonymous_variant')
    syn_ps_ht = syn_ps_ht.group_by(syn_ps_ht.variant_type_model,
                                   syn_ps_ht.mu).aggregate(singleton_count=hl.agg.sum(syn_ps_ht.singleton_count),
                                                           variant_count=hl.agg.sum(syn_ps_ht.variant_count))
    syn_ps_ht = syn_ps_ht.annotate(ps=syn_ps_ht.singleton_count / syn_ps_ht.variant_count)
    assert syn_ps_ht.all(hl.is_defined(syn_ps_ht.mu))

    lm = syn_ps_ht.aggregate(hl.agg.group_by(
        syn_ps_ht.variant_type_model,
        hl.agg.linreg(syn_ps_ht.ps, [1, syn_ps_ht.mu], weight=syn_ps_ht.variant_count)).map_values(lambda x: x.beta))
    lm = hl.literal(lm)
    ht = ht.annotate(expected_singletons=(ht.mu * lm[ht.variant_type_model][1] +
                                          lm[ht.variant_type_model][0]) * ht.variant_count)

    agg_ht = (ht.group_by('worst_csq', *grouping)
              .aggregate(singleton_count=hl.agg.sum(ht.singleton_count),
                         expected_singletons=hl.agg.sum(ht.expected_singletons),
                         variant_count=hl.agg.sum(ht.variant_count)))
    agg_ht = agg_ht.annotate(ps=agg_ht.singleton_count / agg_ht.variant_count,
                             maps=(agg_ht.singleton_count - agg_ht.expected_singletons) / agg_ht.variant_count)
    agg_ht = agg_ht.annotate(maps_sem=(agg_ht.ps * (1 - agg_ht.ps) / agg_ht.variant_count) ** 0.5)
    return agg_ht
