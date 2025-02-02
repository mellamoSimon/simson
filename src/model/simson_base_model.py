import numpy as np
import pandas as pd
import os
import sys
import pickle
import csv
from ODYM.odym.modules.ODYM_Classes import MFAsystem, Classification, Process, Parameter
from src.odym_extension.SimDiGraph_MFAsystem import SimDiGraph_MFAsystem
from src.tools.config import cfg
from src.model.model_tools import get_dsm_data, get_stock_data_country_specific_areas, calc_change_timeline
from src.model.load_dsms import load_dsms
from src.model.load_params import get_cullen_fabrication_yield, get_wittig_distributions
from src.calc_trade.calc_trade import get_trade
from src.calc_trade.calc_scrap_trade import get_scrap_trade
from src.calc_trade.calc_indirect_trade import get_indirect_trade

#  constants: MFA System process IDs

PRIM_PID = 0
USE_PID = 1
EOL_PID = 2
MECH_RECYCLE_PID = 3
INCINERATION_PID = 4

def load_simson_base_model(country_specific=False, recalculate=False, recalculate_dsms=False) -> SimDiGraph_MFAsystem:
    file_name_end = 'countries' if country_specific else f'{cfg.region_data_source}_regions'
    file_name = f'main_model_{file_name_end}.p'
    file_path = os.path.join(cfg.data_path, 'models', file_name)
    do_load_existing = os.path.exists(file_path) and not recalculate
    if do_load_existing:
        model = pickle.load(open(file_path, "rb"))
    else:
        model = create_base_model(country_specific, recalculate_dsms)
        pickle.dump(model, open(file_path, "wb"))
    return model


def create_base_model(country_specific, recalculate_dsms):
    dsms = load_dsms(country_specific, recalculate_dsms)
    model, balance_message = create_model(country_specific, dsms)
    print(balance_message)
    return model


def create_model(country_specific, dsms, scrap_share_in_production=None):
    n_regions = len(dsms)
    max_scrap_share_in_production = _calc_max_scrap_share(scrap_share_in_production, n_regions)
    # load data
    areas = get_stock_data_country_specific_areas(country_specific)
    main_model = set_up_model(areas)
    stocks, inflows, outflows = get_dsm_data(dsms)
    # Load model
    initiate_model(main_model)

    # compute stocks and flows
    compute_flows(main_model, country_specific, inflows, outflows,
                  max_scrap_share_in_production)
    compute_stocks(main_model, stocks, inflows, outflows)

    # check model
    balance_message = mass_balance_plausible(main_model)

    return main_model, balance_message


def initiate_model(main_model):
    initiate_processes(main_model)
    initiate_parameters(main_model)
    initiate_flows(main_model)
    initiate_stocks(main_model)
    main_model.Initialize_FlowValues()
    main_model.Initialize_StockValues()
    check_consistency(main_model)


def set_up_model(regions):
    #why not using 'material' and 'element' dimensions?
    model_classification = {'Time': Classification(Name='Time', Dimension='Time', ID=1,
                                                   Items=cfg.years),
                            'Element': Classification(Name='Elements', Dimension='Element', ID=2, Items=['Fe']),
                            'Region': Classification(Name='Regions', Dimension='Region', ID=3, Items=regions),
                            'Good': Classification(Name='Goods', Dimension='Material', ID=4,
                                                   Items=cfg.in_use_categories),
                            'Waste': Classification(Name='Waste types', Dimension='Material', ID=5,
                                                    Items=cfg.recycling_categories),
                            'Scenario': Classification(Name='Scenario', Dimension='Scenario', ID=6,
                                                       Items=cfg.scenarios)}
    model_time_start = cfg.start_year
    model_time_end = cfg.end_year
    index_table = pd.DataFrame({'Aspect': ['Time', 'Element', 'Region', 'Good', 'Waste', 'Scenario'],
                                'Description': ['Model aspect "Time"', 'Model aspect "Element"',
                                                'Model aspect "Region"', 'Model aspect "Good"',
                                                'Model aspect "Waste"', 'Model aspect "Scenario"'],
                                'Dimension': ['Time', 'Element', 'Region', 'Material', 'Material', 'Scenario'],
                                'Classification': [model_classification[Aspect] for Aspect in
                                                   ['Time', 'Element', 'Region', 'Good', 'Waste', 'Scenario']],
                                'IndexLetter': ['t', 'e', 'r', 'g', 'w', 's']})
    index_table.set_index('Aspect', inplace=True)

    main_model = SimDiGraph_MFAsystem(Name='World Steel Economy',
                                      Geogr_Scope='World',
                                      Unit='t',
                                      ProcessList=[],
                                      FlowDict={},
                                      StockDict={},
                                      ParameterDict={},
                                      Time_Start=model_time_start,
                                      Time_End=model_time_end,
                                      IndexTable=index_table,
                                      Elements=index_table.loc['Element'].Classification.Items)

    return main_model


def initiate_processes(main_model):
    main_model.ProcessList = []

    def add_process(name, p_id):
        main_model.ProcessList.append(Process(Name=name, ID=p_id))

    add_process('Primary Production', PRIM_PID)
    add_process('Mechanical recycling', MECH_RECYCLE_PID)
    add_process('Use phase', USE_PID)
    add_process('End of life', EOL_PID)
    add_process('Incineration', INCINERATION_PID)

def initiate_parameters(main_model):
    parameter_dict = {}

    use_recycling_params, recycling_usable_params = get_wittig_distributions()
    fabrication_yield = get_cullen_fabrication_yield()

    parameter_dict['Fabrication_Yield'] = Parameter(Name='Fabrication_Yield', ID=0,
                                                    P_Res=FABR_PID, MetaData=None, Indices='g',
                                                    Values=np.array(fabrication_yield), Unit='1')

    parameter_dict['Use-EOL_Distribution'] = Parameter(Name='End-of-Life_Distribution', ID=1, P_Res=USE_PID,
                                                       MetaData=None, Indices='gw',
                                                       Values=np.array(use_recycling_params).transpose(), Unit='1')

    parameter_dict['EOL-Recycle_Distribution'] = Parameter(Name='EOL-Recycle_Distribution', ID=2,
                                                           P_Res=SCRAP_PID,
                                                           MetaData=None, Indices='w',
                                                           Values=np.array(recycling_usable_params), Unit='1')

    main_model.ParameterDict = parameter_dict


def initiate_flows(main_model):
    #fix me: gotta decide on the indexes to use. I think it should be material and element
    main_model.init_flow('Primary production - In-Use', PRIM_PID, USE_PID, 't,e,r,s')
    
    main_model.init_flow('Mechanical recycling - In-Use', MECH_RECYCLE_PID, USE_PID, 't,e,r,s')
    # to decribe technical limit of recycling for now:
    main_model.init_flow('Mechanical recycling - Incineration', MECH_RECYCLE_PID, INCINERATION_PID, 't,e,r,s')

    main_model.init_flow('In-Use - Incineration', USE_PID, INCINERATION_PID, 't,e,r,s')
    main_model.init_flow('In-Use - Recycling', USE_PID, MECH_RECYCLE_PID, 't,e,r,s')

def initiate_stocks(main_model):
    main_model.add_stock(USE_PID, 'in_use', 't,e,r,g,s')

def check_consistency(main_model: MFAsystem):
    """
    Uses ODYM consistency checks to see if model dimensions and structure are well
    defined. Raises RuntimeError if not.

    :param main_model: The MFA System
    :return:
    """
    consistency = main_model.Consistency_Check()
    for consistencyCheck in consistency:
        if not consistencyCheck:
            raise RuntimeError("A consistency check failed: " + str(consistency))


def compute_flows(model: MFAsystem, country_specific: bool,
                  inflows: np.ndarray, outflows: np.ndarray, max_scrap_share_in_production: np.ndarray):
    """

    :param model: The MFA system
    :param country_specific:
    :param inflows:
    :param outflows:
    :param max_scrap_share_in_production:
    :return:
    """
    use_eol_distribution, eol_recycle_distribution, fabrication_yield = _get_params(model)

    reuse = None
    if cfg.do_change_reuse:
        # one is substracted as one was added to multiply scenario and category reuse changes
        reuse_factor_timeline = calc_change_timeline(cfg.reuse_factor, cfg.reuse_change_base_year) - 1
        reuse = np.einsum('trgs,tgs->trgs', outflows, reuse_factor_timeline)
        inflows -= reuse
        outflows -= reuse

    total_demand = np.sum(inflows, axis=2)

    indirect_imports, indirect_exports = get_indirect_trade(country_specific=country_specific,
                                                            scaler=total_demand,
                                                            inflows=inflows,
                                                            outflows=outflows)
    direct_inflows = inflows - indirect_imports + indirect_exports

    direct_demand = np.sum(direct_inflows, axis=2)

    inverse_fabrication_yield = 1 / fabrication_yield
    fabrication_by_category = np.einsum('trgs,g->trgs', direct_inflows, inverse_fabrication_yield)
    fabrication = np.sum(fabrication_by_category, axis=2)
    fabrication_scrap = fabrication - direct_demand

    imports, exports = get_trade(country_specific=country_specific, scaler=total_demand)

    forming_fabrication = fabrication
    production_plus_trade = forming_fabrication * (1 / cfg.forming_yield)
    forming_scrap = production_plus_trade - forming_fabrication
    production = production_plus_trade + exports - imports

    outflows_by_waste = np.einsum('trgs,gw->trgws', outflows, use_eol_distribution)
    use_eol = np.zeros_like(outflows_by_waste)
    use_env = np.zeros_like(outflows_by_waste)

    dis_idx = cfg.recycling_categories.index('Dis')
    use_eol[:, :, :, :dis_idx, :] = outflows_by_waste[:, :, :, :dis_idx, :]
    use_env[:, :, :, dis_idx:, :] = outflows_by_waste[:, :, :, dis_idx:, :]
    eol_scrap = np.sum(use_eol, axis=2)

    available_scrap = eol_scrap.copy()
    available_scrap[:, :, cfg.recycling_categories.index('Form'), :] = forming_scrap
    available_scrap[:, :, cfg.recycling_categories.index('Fabr'), :] = fabrication_scrap

    scrap_imports, scrap_exports = get_scrap_trade(country_specific=country_specific, scaler=production,
                                                   available_scrap_by_category=available_scrap)

    total_scrap = available_scrap + scrap_imports - scrap_exports

    max_scrap_in_production = production * max_scrap_share_in_production
    recyclable_scrap = np.einsum('trwe,w->trwe', total_scrap, eol_recycle_distribution)
    recyclable_scrap = np.sum(recyclable_scrap, axis=2)
    scrap_in_production = np.minimum(recyclable_scrap, max_scrap_in_production)

    scrap_share = np.divide(scrap_in_production, production,
                            out=np.zeros_like(scrap_in_production), where=production != 0)
    eaf_share_production = _calc_eaf_share_production(scrap_share)
    eaf_production = production * eaf_share_production
    bof_production = production - eaf_production
    max_scrap_in_bof = cfg.scrap_in_BOF_rate * bof_production
    scrap_in_bof = np.minimum(max_scrap_in_bof, scrap_in_production)
    iron_production = bof_production - scrap_in_bof

    scrap_in_production = scrap_in_bof + eaf_production
    waste = np.sum(total_scrap, axis=2) - scrap_in_production

    edit_flows(model, iron_production, scrap_in_bof, bof_production, eaf_production, forming_fabrication, forming_scrap,
               imports, exports, direct_inflows, reuse, fabrication_scrap, use_eol, use_env, scrap_imports,
               scrap_exports, scrap_in_production, waste, indirect_imports, indirect_exports)

    return model


def edit_flows(model, iron_production, scrap_in_bof, bof_production, eaf_production, forming_fabrication, forming_scrap,
               imports, exports, production_inflows, reuse, fabrication_scrap, use_eol, use_env, scrap_imports,
               scrap_exports, scrap_in_production, waste, indirect_imports, indirect_exports):
    model.get_flowV(ENV_PID, BOF_PID)[:, 0] = iron_production
    model.get_flowV(RECYCLE_PID, BOF_PID)[:, 0] = scrap_in_bof
    model.get_flowV(BOF_PID, FORM_PID)[:, 0] = bof_production
    model.get_flowV(RECYCLE_PID, EAF_PID)[:, 0] = eaf_production
    model.get_flowV(EAF_PID, FORM_PID)[:, 0] = eaf_production
    model.get_flowV(FORM_PID, FABR_PID)[:, 0] = forming_fabrication
    model.get_flowV(FORM_PID, SCRAP_PID)[:, 0, :, cfg.recycling_categories.index('Form')] = forming_scrap
    model.get_flowV(ENV_PID, FORM_PID)[:, 0] = imports
    model.get_flowV(FORM_PID, ENV_PID)[:, 0] = exports
    model.get_flowV(FABR_PID, USE_PID)[:, 0] = production_inflows
    model.get_flowV(ENV_PID, USE_PID)[:, 0] = indirect_imports
    model.get_flowV(USE_PID, ENV_PID)[:, 0] = indirect_exports
    model.get_flowV(FABR_PID, SCRAP_PID)[:, 0, :, cfg.recycling_categories.index('Fabr')] = fabrication_scrap
    if reuse is not None:
        model.get_flowV(USE_PID, USE_PID)[:, 0] = reuse
    model.get_flowV(USE_PID, SCRAP_PID)[:, 0] = use_eol
    model.get_flowV(USE_PID, DISNOTCOL_PID)[:, 0] = use_env
    model.get_flowV(ENV_PID, SCRAP_PID)[:, 0] = scrap_imports
    model.get_flowV(SCRAP_PID, ENV_PID)[:, 0] = scrap_exports
    model.get_flowV(SCRAP_PID, RECYCLE_PID)[:, 0] = scrap_in_production
    model.get_flowV(SCRAP_PID, WASTE_PID)[:, 0] = waste


def _get_params(model):
    params = model.ParameterDict
    use_eol_distribution = params['Use-EOL_Distribution'].Values
    eol_recycle_distribution = params['EOL-Recycle_Distribution'].Values
    fabrication_yield = params['Fabrication_Yield'].Values

    return use_eol_distribution, eol_recycle_distribution, fabrication_yield


def _calc_eaf_share_production(scrap_share):
    eaf_share_production = (scrap_share - cfg.scrap_in_BOF_rate) / (1 - cfg.scrap_in_BOF_rate)
    eaf_share_production[eaf_share_production <= 0] = 0
    return eaf_share_production


def _calc_max_scrap_share(scrap_share_in_production, n_regions):
    max_scrap_share_in_production = np.ones(
        [cfg.n_years, n_regions, cfg.n_scenarios]) * cfg.max_scrap_share_production_base_model
    if scrap_share_in_production is not None:
        max_scrap_share_in_production[cfg.econ_start_index:, :] = scrap_share_in_production
    return max_scrap_share_in_production


def compute_stocks(model, stocks, inflows, outflows):
    in_use_stock = model.get_stockV(USE_PID)
    in_use_stock_change = model.get_stock_changeV(USE_PID)
    in_use_stock[:, 0, :, :] = stocks
    in_use_stock_change[:, 0, :, :] = inflows - outflows

    inflow_waste = model.get_flowV(SCRAP_PID, WASTE_PID)
    model.get_stock_changeV(WASTE_PID)[:] = inflow_waste
    model.calculate_stock_values_from_stock_change(WASTE_PID)

    inflow_disnotcol = model.get_flowV(USE_PID, DISNOTCOL_PID)
    inflow_disnotcol = np.sum(inflow_disnotcol, axis=3)
    model.get_stock_changeV(DISNOTCOL_PID)[:] = inflow_disnotcol
    model.calculate_stock_values_from_stock_change(DISNOTCOL_PID)

    return model


def mass_balance_plausible(main_model):
    """
    Checks if a given mass balance is plausible.
    :return: True if the mass balance for all processes is below 1t of steel, False otherwise.
    """

    balance = main_model.MassBalance()
    for val in np.abs(balance).sum(axis=0).sum(axis=1):
        if val > 1:
            raise RuntimeError(
                "Error, Mass Balance summary below\n" + str(np.abs(balance).sum(axis=0).sum(axis=1)))
    return f"Success - Model loaded and checked. \nBalance: {str(list(np.abs(balance).sum(axis=0).sum(axis=1)))}.\n"


def main():
    """
    Recalculates the DMFA dict based on the dynamic stock models and trade_all_areas data.
    Checks the Mass Balance and raises a runtime error if the mass balance is too big.
    Prints success statements otherwise
    :return: None
    """
    load_simson_base_model(country_specific=False, recalculate=True)


if __name__ == "__main__":
    # overwrite config with values given in a config file,
    # if the path to this file is passed as the last argument of the function call.
    if sys.argv[-1].endswith('.yml'):
        cfg.customize(sys.argv[-1])
    main()
