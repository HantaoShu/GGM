from collections import OrderedDict
from operator import itemgetter

import numpy as np
from rdkit import Chem
from rdkit.Chem.EnumerateStereoisomers \
    import EnumerateStereoisomers, StereoEnumerationOptions
import torch
import torch.nn as nn

ATOM_SYMBOLS = ['C', 'N', 'O', 'S', 'F', 'P', 'Cl', 'Br', 'D']
N_atom_features = len(ATOM_SYMBOLS)
N_bond_features = 5  # See `utils.bond_features`
N_extra_atom_features = 5  # See `utils.atom_features` and `utils.make_graph`
N_extra_bond_features = 6  # See `utils.bond_features`


def initialize_model(model, load_save_file=False):
    """\
    Parameters
    ----------
    model: ggm.ggm
    load_save_file: str
        File path of the save model.
    """
    if load_save_file:
        model.load_state_dict(torch.load(load_save_file))
    else:
        for param in model.parameters():
            if param.dim() == 1:
                continue
                nn.init.constant(param, 0)
            else:
                #nn.init.normal(param, 0.0, 0.15)
                nn.init.xavier_normal(param)
    return model

def create_var(tensor, requires_grad=False):
    """\
    create_var(...) -> torch.autograd.Variable

    Wrap a torch.Tensor object by torch.autograd.Variable.
    """
    tensor.requires_grad = requires_grad
    return tensor


def one_of_k_encoding_unk(x, allowable_set):
    """Maps inputs not in the allowable set to the last element."""
    """\
    one_of_k_encoding_unk(...) -> list[int]

    One-hot encode `x` based on `allowable_set`.
    Return None if `x not in allowable_set`.
    """
    if x not in allowable_set:
        return None
    return list(map(lambda s: int(x == s), allowable_set))


def one_hot(tensor, depth):
    """\
    Return an one-hot vector given an index and length.

    Parameters
    ----------
    tensor: torch.FloatTensor of shape (1,)
        A 0-D tensor containing only an index.
    depth: int
        The length of the resulting one-hot.

    Returns
    -------
    torch.FloatTensor of shape (1, depth)
    """
    ones = torch.sparse.torch.eye(depth)
    return ones.index_select(0, tensor.long())

def collect_node_state(h, except_last=False):
    """Return a matrix made by concatenating the node vectors.
    Return shape -> (len(h), node_vector_length)    # if not except_last
                    (len(h)-1, node_vector_length)  # if except_last
    """
    retval = []
    for i in list(h.keys())[:-1]:
        retval.append(h[i])
    if not except_last:
        retval.append(h[list(h.keys())[-1]])
    return torch.cat(retval, 0)


def atom_features(atom, include_extra=False):
    """\
    atom_features(...) -> list[int]

    One-hot encode `atom` w/ or w/o extra concatenation.
    """
    retval = one_of_k_encoding_unk(atom.GetSymbol(), ATOM_SYMBOLS)
    if include_extra:
        retval += [atom.GetDegree(), atom.GetFormalCharge()]
    return retval


def bond_features(bond, include_extra=False):
    """\
    bond_features(...) -> list[int]

    One-hot encode `bond` w/ or w/o extra concatenation.

    Parameters
    ----------
    bond: rdkit.Chem.Bond
    include_extra: bool
    """
    bt = bond.GetBondType()  # rdkit.Chem.BondType
    retval = [
      bt == Chem.rdchem.BondType.SINGLE, bt == Chem.rdchem.BondType.DOUBLE,
      bt == Chem.rdchem.BondType.TRIPLE, bt == Chem.rdchem.BondType.AROMATIC,
      0 # no bond
      ]
    if include_extra:
        bs = bond.GetStereo()
        retval += [bs == Chem.rdchem.BondStereo.STEREONONE,
                   bs == Chem.rdchem.BondStereo.STEREOANY,
                   bs == Chem.rdchem.BondStereo.STEREOZ,
                   bs == Chem.rdchem.BondStereo.STEREOE,
                   bs == Chem.rdchem.BondStereo.STEREOCIS,
                   bs == Chem.rdchem.BondStereo.STEREOTRANS
                  ]
    return np.array(retval)


def make_graph(smiles, extra_atom_feature = False, extra_bond_feature = False):
    """\
    make_graph(...) -> edge_dict, node_dict

    Construct a graph representation of a given SMILES.

    The nodes and edges are represented as one-hot vectors,
    which are respectively gathered into a node dict and an edge dict
    having atom indices as their keys.
    See below for the exact return structures.
    This graph representation is constantly used in the methods of `ggm.ggm`.

    Returns
    -------
    g: OrderedDict[int, list[tuple[torch.autogra.Variable, int]]] | None
        Edge (bond) dictionary, which looks like:
            { atom_idx: [ (one_hot_vector, partner_atom_idx), ... ], ... }
    h: OrderedDict[int, torch.autograd.Variable] | None
        Node (atom) dictionary, which looks like:
            { atom_idx: one_hot_vector, ... }
    If untreatable atoms are present, return (None, None).
    """
    g = OrderedDict({})
    h = OrderedDict({})
    if type(smiles) is str or type(smiles) is np.str_:
        molecule = Chem.MolFromSmiles(smiles)
    else:
        molecule = smiles
    chiral_list = Chem.FindMolChiralCenters(molecule)
    chiral_index = [c[0] for c in chiral_list]
    chiral_tag = [c[1] for c in chiral_list]
    for i in range(0, molecule.GetNumAtoms()):
        atom_i = molecule.GetAtomWithIdx(i)  # rdkit.Chem.Atom
        if atom_i.GetSymbol() not in ATOM_SYMBOLS:
            return None, None
        atom_i = atom_features(atom_i, extra_atom_feature)  # One-hot vector
        if extra_atom_feature:
            if i in chiral_index:
                if chiral_tag[chiral_index.index(i)] == 'R':
                    atom_i += [0, 1, 0]
                else:
                    atom_i += [0, 0, 1]
            else:
                atom_i += [1, 0, 0]
        h[i] = create_var(torch.FloatTensor(atom_i), False).view(1, -1)
        for j in range(0, molecule.GetNumAtoms()):
            e_ij = molecule.GetBondBetweenAtoms(i, j)  # rdkit.Chem.Bond
            if e_ij is not None:
                # ADDED edge feat; one-hot vector
                e_ij = list(map(lambda x: 1 if x else 0,
                                bond_features(e_ij, extra_bond_feature)))
                e_ij = create_var(torch.FloatTensor(e_ij).view(1, -1), False)
                # atom_j = molecule.GetAtomWithIdx(j) not used
                if i not in g:
                    g[i] = []
                g[i].append( (e_ij, j) )
    return g, h


def make_graphs(s1, s2, extra_atom_feature = False, extra_bond_feature = False):
    """\
    make_graphs(...) -> edge_dict_1, node_dict_1, edge_dict_2, node_dict_2

    Similar to `make_graph`, but adjust the node indices of `s1` according to `s2`
    (refer to `index_rearrange` for details).
    Typically, `s1` is a whole SMILES and `s2` a scaffold SMILES.
    """
    molecule1 = Chem.MolFromSmiles(s1)
    molecule2 = Chem.MolFromSmiles(s2)
    g1, h1 = \
        make_graph(Chem.Mol(molecule1), extra_atom_feature, extra_bond_feature)
    g2, h2 = \
        make_graph(Chem.Mol(molecule2), extra_atom_feature, extra_bond_feature)
    if g1 is None or h1 is None or g2 is None or h2 is None:
        return None, None, None, None
    try:
        g1, h1 = index_rearrange(molecule1, molecule2, g1, h1)
    except:
        return None, None, None, None
    return g1, h1, g2, h2


def is_equal_node_type(h1, h2):
    if len(h1) != len(h2):
        return False
    for i in h1:
        if not np.array_equal(h1[i].data.cpu().numpy(), h2[i].data.cpu().numpy()):
            return False
    return True


def is_equal_edge_type(g1, g2):
    if len(g1) != len(g2):
        return False
    for i in g1:
        if len(g1[i])!=len(g2[i]):
            return False
        sorted1 = sorted(g1[i], key=itemgetter(1))
        sorted2 = sorted(g2[i], key=itemgetter(1))
        for j in range(len(sorted1)):
            if not np.array_equal(sorted1[j][0].data.cpu().numpy(), sorted2[j][0].data.cpu().numpy()):
                print (i, j, sorted1[j][0].data.cpu().numpy())
                print (i, j, sorted2[j][0].data.cpu().numpy())
                return False
            if sorted1[j][1]!=sorted2[j][1]:
                print (i, j, sorted1[j][1])
                print (i, j, sorted2[j][1])
                return False
    return True


def ensure_shared_grads(model, shared_model, gpu=False):
    """Make a shared model have the gradients of a worker model.
    Used in `vaetrain.train` for multiprocessed optimization."""
    for param, shared_param in zip(model.parameters(), shared_model.parameters()):
        if shared_param.grad is not None and not gpu:
            return
        elif not gpu:
            shared_param._grad = param.grad
        else:
            if param.grad is None:
                continue
            shared_param._grad = param.grad.cpu()


def index_rearrange(molecule1, molecule2, g, h):
    """\
    index_rearrange(...) -> edge_dict, node_dict

    By comparing `molecule1` and a substructure `molecule2`,
    make the overlapping atoms of `molecule1` have the same indices as in `molecule2`:

        molecule1   molecule2      molecule1
        H - N - C     N - C    ->  H - N - C
        0   1   2     1   0        2   1   0

    and apply the rearrangement to `g` and `h`.
    Note that the order of `g.values()` and `h.values()` is preserved.

    Parameters
    ----------
    molecule1: rdkit.Chem.Mol
    molecule2: rdkit.Chem.Mol
    g: edge dict of molecule1
    h: node dict of molecule1
    """
    # The indices of `molecule1` atoms that overlap `molecule2` atoms.
    # The returned ordering corresponds to the atom ordering of `molecule2`.
    scaffold_index = list(molecule1.GetSubstructMatches(molecule2)[0])
    new_index = OrderedDict({})  # Does `new_index` have to be an "ordered" dict?
    for idx,i in enumerate(scaffold_index):
        new_index[i]=idx  # new_index[index_in_molecule1] -> index_in_molecule2
    # Shift to the end the indices of the left atoms in `molecule1`.
    idx = len(scaffold_index)
    for i in range(len(h)):
        if i not in scaffold_index:
            new_index[i] = idx
            idx+=1
    g, h = index_change(g, h, new_index)
    return g, h


def index_change(g, h, new_index):
    """\
    index_change(...) -> edge_dict, node_dict

    Rearrange the node numbering of `g` and `h` according to the mapping by `new_index`.

    Parameters
    ----------
    new_index: dict[int, int]
    """
    new_h = OrderedDict({})
    new_g = OrderedDict({})
    for i in h.keys():
        new_h[new_index[i]]=h[i]
    for i in g.keys():
        new_g[new_index[i]]=[]
        for j in g[i]:
            new_g[new_index[i]].append((j[0], new_index[j[1]]))
    return new_g, new_h


def ensure_shared_grads(model, shared_model, gpu=False):
    """Make a shared model have the gradients of a worker model.
    Used in `vaetrain.train` for multiprocessed optimization."""
    for param, shared_param in zip(model.parameters(), shared_model.parameters()):
        if shared_param.grad is not None and not gpu:
            return
        elif not gpu:
            shared_param._grad = param.grad
        else:
            if param.grad is None:
                continue
            shared_param._grad = param.grad.cpu()


def average_node_state(h):
    """Return the element-wise mean of the node vectors."""
    #retval = create_var(torch.zeros(1,9))
    retval = create_var(torch.zeros(h[0].size()))
    for i in list(h.keys()):
        retval+=h[i]
    if len(h)>0:
        retval=retval/len(h)
    return retval


def enumerate_molecule(s: str):
    """Return a list of all the isomer SMILESs of a given SMILES `s`."""
    m = Chem.MolFromSmiles(s)
    opts = StereoEnumerationOptions(unique=True, onlyUnassigned=False)
    #opts = StereoEnumerationOptions(tryEmbedding=True, unique=True, onlyUnassigned=False)
    isomers = tuple(EnumerateStereoisomers(m, options=opts))
    retval = []
    for smi in sorted(Chem.MolToSmiles(x,isomericSmiles=True) for x in isomers):
        retval.append(smi)
    return retval


def copy_ordered_dict(original):
    copied = OrderedDict()
    print(original.keys())
    print(original.values())
