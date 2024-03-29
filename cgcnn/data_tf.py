from __future__ import print_function, division

import csv
import functools
import json
import os
import random
import warnings

import numpy as np
from pymatgen.core.structure import Structure
import tensorflow as tf
import math

try:
    import ray
    ray.init()
    use_ray = True
except:
    print("Recommend to install ray for accelerate processing")
    use_ray = False

def result_dir_make(target_path):
    try:
        if not os.path.exists(target_path):
            print("Make result directory %s"%(target_path))
            os.makedirs(target_path)
            #print("")
    except OSError:
        print('Error: Failed to create directory : ' +  target_path)


class Dataloader_original(tf.keras.utils.Sequence):

    def __init__(self, dataset, batch_size=1, shuffle=False, return_id=False, drop_remainder=False):
        self.dataset = dataset
        self.return_id = return_id
        self.batch_size = batch_size
        self.drop_remainder = drop_remainder
        self.shuffle=shuffle
        if self.drop_remainder == True:
            self.shuffle == True
        self.on_epoch_end()

    def __len__(self):
        return math.ceil(len(self.dataset) / self.batch_size)

    def __getitem__(self, idx):
        indices = self.indices[idx]

        if self.use_ray == True:
            batch_dataset = ray.get([CIFData_from_DataFrame_ray.remote(self.dataset.iloc[i]) for i in indices])
        else:
            batch_dataset = [self.dataset[i] for i in indices]
        batch_x, batch_y, batch_id = collate_pool(batch_dataset)

        if self.return_id == False:
            return batch_x, batch_y
        else:
            return batch_x, batch_y, batch_id

    # epoch 끝날때마다 실행
    def on_epoch_end(self):
        indice = tf.data.Dataset.range(len(self.dataset)) 
        indice = indice.shuffle(len(self.dataset), reshuffle_each_iteration=self.shuffle)
        batch_indices = indice.batch(self.batch_size, drop_remainder=self.drop_remainder)
        self.indices = list(batch_indices.as_numpy_iterator())


class Dataloader_ray(tf.keras.utils.Sequence):

    def __init__(self, dataset, batch_size=1, shuffle=False, return_id=False, drop_remainder=False):
        self.dataset = dataset
        self.return_id = return_id
        self.batch_size = batch_size
        self.drop_remainder = drop_remainder
        self.shuffle=shuffle
        if self.drop_remainder == True:
            self.shuffle == True
        self.on_epoch_end()

        self.CIFData_from_DataFrame_ray = ray.remote(CIFData_from_DataFrame_ray)

    def __len__(self):
        return math.ceil(len(self.dataset) / self.batch_size)

    def __getitem__(self, idx):
        indices = self.indices[idx]

        batch_dataset = ray.get([self.CIFData_from_DataFrame_ray.remote(self.dataset.iloc[i]) for i in indices])
        batch_x, batch_y, batch_id = collate_pool(batch_dataset)

        if self.return_id == False:
            return batch_x, batch_y
        else:
            return batch_x, batch_y, batch_id

    # epoch 끝날때마다 실행
    def on_epoch_end(self):
        indice = tf.data.Dataset.range(len(self.dataset)) 
        indice = indice.shuffle(len(self.dataset), reshuffle_each_iteration=self.shuffle)
        batch_indices = indice.batch(self.batch_size, drop_remainder=self.drop_remainder)
        self.indices = list(batch_indices.as_numpy_iterator())


def Dataloader(dataset, batch_size=1, shuffle=False, return_id=False, drop_remainder=False):
    if use_ray == False:
        return Dataloader_original(dataset, batch_size=batch_size, shuffle=shuffle, return_id=return_id, drop_remainder=drop_remainder)
    else:
        return Dataloader_ray(dataset, batch_size=batch_size, shuffle=shuffle, return_id=return_id, drop_remainder=drop_remainder)


def collate_pool(dataset_list):
    """
    Collate a list of data and return a batch for predicting crystal
    properties.

    Parameters
    ----------

    dataset_list: list of tuples for each data point.
      (atom_fea, nbr_fea, nbr_fea_idx, target)

      atom_fea: tf.Tensor shape (n_i, atom_fea_len)
      nbr_fea: tf.Tensor shape (n_i, M, nbr_fea_len)
      nbr_fea_idx: torch.LongTensor shape (n_i, M)
      target: tf.Tensor shape (1, )
      cif_id: str or int

    Returns
    -------
    N = sum(n_i); N0 = sum(i)

    batch_atom_fea: tf.Tensor shape (N, orig_atom_fea_len)
      Atom features from atom type
    batch_nbr_fea: tf.Tensor shape (N, M, nbr_fea_len)
      Bond features of each atom's M neighbors
    batch_nbr_fea_idx: torch.LongTensor shape (N, M)
      Indices of M neighbors of each atom
    crystal_atom_idx: list of torch.LongTensor of length N0
      Mapping from the crystal idx to atom idx
    target: tf.Tensor shape (N, 1)
      Target value for prediction
    batch_cif_ids: list
    """
    batch_atom_fea, batch_nbr_fea, batch_nbr_fea_idx = [], [], []
    crystal_atom_idx, batch_target = [], []
    batch_cif_ids = []
    base_idx = 0
    for i, ((atom_fea, nbr_fea, nbr_fea_idx), target, cif_id)\
            in enumerate(dataset_list):
        n_i = atom_fea.shape[0]  # number of atoms for this crystal
        batch_atom_fea.append(atom_fea)
        batch_nbr_fea.append(nbr_fea)
        batch_nbr_fea_idx.append(nbr_fea_idx+base_idx)
        new_idx = tf.constant(np.arange(n_i)+base_idx)
        crystal_atom_idx.append(new_idx)
        batch_target.append(target)
        batch_cif_ids.append(cif_id)
        base_idx += n_i
    return (tf.concat(batch_atom_fea, axis=0),
            tf.concat(batch_nbr_fea, axis=0),
            tf.concat(batch_nbr_fea_idx, axis=0),
            crystal_atom_idx),\
        tf.stack(batch_target, axis=0),\
        batch_cif_ids


class GaussianDistance(object):
    """
    Expands the distance by Gaussian basis.

    Unit: angstrom
    """
    def __init__(self, dmin, dmax, step, var=None):
        """
        Parameters
        ----------

        dmin: float
          Minimum interatomic distance
        dmax: float
          Maximum interatomic distance
        step: float
          Step size for the Gaussian filter
        """
        assert dmin < dmax
        assert dmax - dmin > step
        self.filter = np.arange(dmin, dmax+step, step)
        if var is None:
            var = step
        self.var = var

    def expand(self, distances):
        """
        Apply Gaussian disntance filter to a numpy distance array

        Parameters
        ----------

        distance: np.array shape n-d array
          A distance matrix of any shape

        Returns
        -------
        expanded_distance: shape (n+1)-d array
          Expanded distance matrix with the last dimension of length
          len(self.filter)
        """
        return np.exp(-(distances[..., np.newaxis] - self.filter)**2 /
                      self.var**2)


class AtomInitializer(object):
    """
    Base class for intializing the vector representation for atoms.

    !!! Use one AtomInitializer per dataset !!!
    """
    def __init__(self, atom_types):
        self.atom_types = set(atom_types)
        self._embedding = {}

    def get_atom_fea(self, atom_type):
        assert atom_type in self.atom_types
        return self._embedding[atom_type]

    def load_state_dict(self, state_dict):
        self._embedding = state_dict
        self.atom_types = set(self._embedding.keys())
        self._decodedict = {idx: atom_type for atom_type, idx in
                            self._embedding.items()}

    def state_dict(self):
        return self._embedding

    def decode(self, idx):
        if not hasattr(self, '_decodedict'):
            self._decodedict = {idx: atom_type for atom_type, idx in
                                self._embedding.items()}
        return self._decodedict[idx]


class AtomCustomJSONInitializer(AtomInitializer):
    """
    Initialize atom feature vectors using a JSON file, which is a python
    dictionary mapping from element number to a list representing the
    feature vector of the element.

    Parameters
    ----------

    elem_embedding_file: str
        The path to the .json file
    """
    def __init__(self, elem_embedding_file):
        with open(elem_embedding_file) as f:
            elem_embedding = json.load(f)
        elem_embedding = {int(key): value for key, value
                          in elem_embedding.items()}
        atom_types = set(elem_embedding.keys())
        super(AtomCustomJSONInitializer, self).__init__(atom_types)
        for key, value in elem_embedding.items():
            self._embedding[key] = np.array(value, dtype=float)


class CIFData(tf.keras.utils.Sequence):
    """
    The CIFData dataset is a wrapper for a dataset where the crystal structures
    are stored in the form of CIF files. The dataset should have the following
    directory structure:

    root_dir
    ├── id_prop.csv
    ├── atom_init.json
    ├── id0.cif
    ├── id1.cif
    ├── ...

    id_prop.csv: a CSV file with two columns. The first column recodes a
    unique ID for each crystal, and the second column recodes the value of
    target property.

    atom_init.json: a JSON file that stores the initialization vector for each
    element.

    ID.cif: a CIF file that recodes the crystal structure, where ID is the
    unique ID for the crystal.

    Parameters
    ----------

    root_dir: str
        The path to the root directory of the dataset
    max_num_nbr: int
        The maximum number of neighbors while constructing the crystal graph
    radius: float
        The cutoff radius for searching neighbors
    dmin: float
        The minimum distance for constructing GaussianDistance
    step: float
        The step size for constructing GaussianDistance
    random_seed: int
        Random seed for shuffling the dataset

    Returns
    -------

    atom_fea: tf.Tensor shape (n_i, atom_fea_len)
    nbr_fea: tf.Tensor shape (n_i, M, nbr_fea_len)
    nbr_fea_idx: torch.LongTensor shape (n_i, M)
    target: tf.Tensor shape (1, )
    cif_id: str or int
    """
    def __init__(self, root_dir, max_num_nbr=12, radius=8, dmin=0, step=0.2,
                 random_seed=123):
        self.root_dir = root_dir
        self.max_num_nbr, self.radius = max_num_nbr, radius
        assert os.path.exists(root_dir), 'root_dir does not exist!'
        id_prop_file = os.path.join(self.root_dir, 'id_prop.csv')
        assert os.path.exists(id_prop_file), 'id_prop.csv does not exist!'
        with open(id_prop_file) as f:
            reader = csv.reader(f)
            self.id_prop_data = [row for row in reader]
        random.seed(random_seed)
        random.shuffle(self.id_prop_data)
        atom_init_file = os.path.join(self.root_dir, 'atom_init.json')
        assert os.path.exists(atom_init_file), 'atom_init.json does not exist!'
        self.ari = AtomCustomJSONInitializer(atom_init_file)
        self.gdf = GaussianDistance(dmin=dmin, dmax=self.radius, step=step)

    def __len__(self):
        return len(self.id_prop_data)

    @functools.lru_cache(maxsize=None)  # Cache loaded structures
    def __getitem__(self, idx):
        cif_id, target = self.id_prop_data[idx]
        crystal = Structure.from_file(os.path.join(self.root_dir,
                                                   cif_id+'.cif'))
        atom_fea = np.vstack([self.ari.get_atom_fea(crystal[i].specie.number)
                              for i in range(len(crystal))])
        atom_fea = tf.constant(atom_fea)
        all_nbrs = crystal.get_all_neighbors(self.radius, include_index=True)
        all_nbrs = [sorted(nbrs, key=lambda x: x[1]) for nbrs in all_nbrs]
        nbr_fea_idx, nbr_fea = [], []
        for nbr in all_nbrs:
            if len(nbr) < self.max_num_nbr:
                warnings.warn('{} not find enough neighbors to build graph. '
                              'If it happens frequently, consider increase '
                              'radius.'.format(cif_id))
                nbr_fea_idx.append(list(map(lambda x: x[2], nbr)) +
                                   [0] * (self.max_num_nbr - len(nbr)))
                nbr_fea.append(list(map(lambda x: x[1], nbr)) +
                               [self.radius + 1.] * (self.max_num_nbr -
                                                     len(nbr)))
            else:
                nbr_fea_idx.append(list(map(lambda x: x[2],
                                            nbr[:self.max_num_nbr])))
                nbr_fea.append(list(map(lambda x: x[1],
                                        nbr[:self.max_num_nbr])))
        nbr_fea_idx, nbr_fea = np.array(nbr_fea_idx), np.array(nbr_fea)
        nbr_fea = self.gdf.expand(nbr_fea)
        atom_fea = tf.constant(atom_fea, dtype=tf.float64)
        nbr_fea = tf.constant(nbr_fea, dtype=tf.float64)
        nbr_fea_idx = tf.constant(nbr_fea_idx)
        target = tf.constant([float(target)])
        return (atom_fea, nbr_fea, nbr_fea_idx), target, cif_id


class CIFData_from_DataFrame(tf.keras.utils.Sequence):
    """
    The CIFData dataset is a wrapper for a dataset where the crystal structures
    are stored in the form of CIF files. The dataset should have the following
    directory structure:

    root_dir
    ├── id_prop.csv
    ├── atom_init.json
    ├── id0.cif
    ├── id1.cif
    ├── ...

    id_prop.csv: a CSV file with two columns. The first column recodes a
    unique ID for each crystal, and the second column recodes the value of
    target property.

    atom_init.json: a JSON file that stores the initialization vector for each
    element.

    ID.cif: a CIF file that recodes the crystal structure, where ID is the
    unique ID for the crystal.

    Parameters
    ----------

    root_dir: str
        The path to the root directory of the dataset
    max_num_nbr: int
        The maximum number of neighbors while constructing the crystal graph
    radius: float
        The cutoff radius for searching neighbors
    dmin: float
        The minimum distance for constructing GaussianDistance
    step: float
        The step size for constructing GaussianDistance
    random_seed: int
        Random seed for shuffling the dataset

    Returns
    -------

    atom_fea: tf.Tensor shape (n_i, atom_fea_len)
    nbr_fea: tf.Tensor shape (n_i, M, nbr_fea_len)
    nbr_fea_idx: torch.LongTensor shape (n_i, M)
    target: tf.Tensor shape (1, )
    cif_id: str or int
    """
    def __init__(self, dataframe, max_num_nbr=12, radius=8, dmin=0, step=0.2,
                 random_seed=123):
        self.material_ids = dataframe['id'].values
        self.targets = dataframe['target'].values
        self.cifs = dataframe['cif'].values
        self.max_num_nbr, self.radius = max_num_nbr, radius

        atom_init_file = os.path.join('atom_init.json')
        assert os.path.exists(atom_init_file), 'atom_init.json does not exist!'
        self.ari = AtomCustomJSONInitializer(atom_init_file)
        self.gdf = GaussianDistance(dmin=dmin, dmax=self.radius, step=step)

    def __len__(self):
        return len(self.material_ids)

    @functools.lru_cache(maxsize=None)  # Cache loaded structures
    def __getitem__(self, idx):
        cif_id = self.material_ids[idx]
        target = self.targets[idx]
        crystal = Structure.from_str(self.cifs[idx], 'cif')
        atom_fea = np.vstack([self.ari.get_atom_fea(crystal[i].specie.number)
                              for i in range(len(crystal))])
        atom_fea = tf.constant(atom_fea)
        all_nbrs = crystal.get_all_neighbors(self.radius, include_index=True)
        all_nbrs = [sorted(nbrs, key=lambda x: x[1]) for nbrs in all_nbrs]
        nbr_fea_idx, nbr_fea = [], []
        for nbr in all_nbrs:
            if len(nbr) < self.max_num_nbr:
                warnings.warn('{} not find enough neighbors to build graph. '
                              'If it happens frequently, consider increase '
                              'radius.'.format(cif_id))
                nbr_fea_idx.append(list(map(lambda x: x[2], nbr)) +
                                   [0] * (self.max_num_nbr - len(nbr)))
                nbr_fea.append(list(map(lambda x: x[1], nbr)) +
                               [self.radius + 1.] * (self.max_num_nbr -
                                                     len(nbr)))
            else:
                nbr_fea_idx.append(list(map(lambda x: x[2],
                                            nbr[:self.max_num_nbr])))
                nbr_fea.append(list(map(lambda x: x[1],
                                        nbr[:self.max_num_nbr])))
        nbr_fea_idx, nbr_fea = np.array(nbr_fea_idx), np.array(nbr_fea)
        nbr_fea = self.gdf.expand(nbr_fea)
        atom_fea = tf.constant(atom_fea, dtype=tf.float64)
        nbr_fea = tf.constant(nbr_fea, dtype=tf.float64)
        nbr_fea_idx = tf.constant(nbr_fea_idx)
        target = tf.constant([float(target)])
        return (atom_fea, nbr_fea, nbr_fea_idx), target , cif_id


#@ray.remote
def CIFData_from_DataFrame_ray(data, max_num_nbr=12, radius=8, dmin=0, step=0.2):
    atom_init_file = os.path.join('atom_init.json')
    assert os.path.exists(atom_init_file), 'atom_init.json does not exist!'
    
    ari = AtomCustomJSONInitializer(atom_init_file)
    gdf = GaussianDistance(dmin=dmin, dmax=radius, step=step)

    material_id = data["id"]
    target = data["target"]
    cif = data["cif"]
    crystal = Structure.from_str(cif, 'cif')
    atom_fea = np.vstack([ari.get_atom_fea(crystal[i].specie.number)
                          for i in range(len(crystal))])
    #atom_fea = tf.constant(atom_fea)
    all_nbrs = crystal.get_all_neighbors(radius, include_index=True)
    all_nbrs = [sorted(nbrs, key=lambda x: x[1]) for nbrs in all_nbrs]
    nbr_fea_idx, nbr_fea = [], []
    for nbr in all_nbrs:
        if len(nbr) < max_num_nbr:
            warnings.warn('{} not find enough neighbors to build graph. '
                          'If it happens frequently, consider increase '
                          'radius.'.format(material_id))
            nbr_fea_idx.append(list(map(lambda x: x[2], nbr)) +
                               [0] * (max_num_nbr - len(nbr)))
            nbr_fea.append(list(map(lambda x: x[1], nbr)) +
                           [radius + 1.] * (max_num_nbr -
                                                 len(nbr)))
        else:
            nbr_fea_idx.append(list(map(lambda x: x[2],
                                        nbr[:max_num_nbr])))
            nbr_fea.append(list(map(lambda x: x[1],
                                    nbr[:max_num_nbr])))
    nbr_fea_idx, nbr_fea = np.array(nbr_fea_idx), np.array(nbr_fea)
    nbr_fea = gdf.expand(nbr_fea)
    #atom_fea = tf.constant(atom_fea, dtype=tf.float64)
    #nbr_fea = tf.constant(nbr_fea, dtype=tf.float64)
    #nbr_fea_idx = tf.constant(nbr_fea_idx)
    #target = tf.constant([float(target)])
    return (atom_fea, nbr_fea, nbr_fea_idx) , target , material_id