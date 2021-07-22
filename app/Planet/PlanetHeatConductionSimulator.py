
import numpy as np

from fealpy.decorator import cartesian, barycentric
from fealpy.functionspace import ParametricLagrangeFiniteElementSpaceOnWedgeMesh
from fealpy.timeintegratoralg.timeline import UniformTimeLine

from scipy.sparse import csr_matrix
from scipy.sparse.linalg import spsolve

class PlanetHeatConductionSimulator():
    def __init__(self, pde, mesh, args):

        self.args = args
        self.pde = pde
        self.mesh = mesh
        self.space = ParametricLagrangeFiniteElementSpaceOnWedgeMesh(mesh,
                p=args.degree, q=args.nq)
        self.S = self.space.stiff_matrix() # 刚度矩阵
        self.M = self.space.mass_matrix() # 质量矩阵

        omega = self.pde.options['omega']
        self.args.T *= 3600*24 # 换算成秒
        self.args.T *= omega # 归一化
        self.args.DT *= omega 
        NT = int(args.T/args.DT)
        self.timeline = UniformTimeLine(0, args.T, NT)

        self.uh0 = self.space.function() # 当前层的数值解
        self.uh0[:] = self.pde.options['theta']/self.pde.options['Tss']
        self.uh1 = self.space.function() # 下一层的数值解

        self.uh = self.space.function() # 临时数值解
        self.uh[:] = self.uh0
    
    def init_mu(self, bcs):
        sd = self.pde.options['sd']

        t = self.timeline.next_time_level()
        index = self.mesh.ds.exterior_boundary_tface_index()
        m = self.mesh.boundary_tri_face_unit_normal(bcs, index=index)

        # 指向太阳的向量绕 z 轴旋转, 这里 t 为 omega*t
        Z = np.array([[np.cos(-t), -np.sin(-t), 0],
            [np.sin(-t), np.cos(-t), 0],
            [0, 0, 1]], dtype=np.float64)
        n = Z@sd # t 时刻指向太阳的方向
        n = n/np.sqrt(np.dot(n, n)) # 单位化处理

        mu = np.dot(m, n)
        mu[mu<0] = 0
        return mu

    def nolinear_robin_boundary(self):
        """

        Notes
        -----
        处理非线性 Robin 边界条件

        """

        uh = self.uh # 临时的温度有限元函数
        mesh = self.mesh
        Phi = self.pde.options['Phi']
        t1 = self.timeline.next_time_level()

        qf = mesh.integrator(self.args.nq, 'tface')
        bcs, ws = qf.get_quadrature_points_and_weights()
        phi = self.space.basis(bcs)

        index = mesh.ds.exterior_boundary_tface_index()
        face2dof = self.space.tri_face_to_dof()[index]

        val = np.einsum('qfi, fi->qf', phi, uh[face2dof])
        val **= 3
        kappa = -1.0/Phi 
        val *=kappa

        measure = mesh.boundary_tri_face_area(index=index)

        pp = mesh.bc_to_point(bcs, etype='face', ftype='tri', index=index)
        n = mesh.boundary_tri_face_unit_normal(bcs, index=index)

        mu = self.init_mu(bcs)
        gR = -mu/Phi # (NQ, NF, ...)

        bb = np.einsum('q, qf, qfi, f->fi', ws, gR, phi, measure)
        
        b = np.zeros(len(uh), dtype=np.float64)
        np.add.at(b, face2dof, bb)

        FM = np.einsum('q, qf, qfi, qfj, f->fij', ws, val, phi, phi, measure)

        I = np.broadcast_to(face2dof[:, :, None], shape=FM.shape)
        J = np.broadcast_to(face2dof[:, None, :], shape=FM.shape)

        R = csr_matrix((FM.flat, (I.flat, J.flat)), shape=self.S.shape)
        return R+self.S, b

    def picard_iteration(self, ctx=None):
        '''  picard 迭代  向后欧拉方法 '''

        timeline = self.timeline
        dt = timeline.current_time_step_length()

        M = self.M

        uh0 = self.uh0
        uh1 = self.uh1
        uh = self.uh

        uh1[:] = uh0
        
        e = self.args.accuracy
        m = self.args.npicard
        k = 0
        error = 1.0
        while error > e:
            R, b = self.nolinear_robin_boundary()
            b *= dt
            b += M@uh0[:]
            R *= dt
            R += M

            if ctx is None:
                uh[:] = spsolve(R, b).reshape(-1)
            else:
                if ctx.myid == 0:
                    ctx.set_centralized_sparse(R)
                    x = b.copy()
                    ctx.set_rhs(x) # Modified in place
                ctx.run(job=6)
                uh[:] = x

            error = np.max(np.abs(uh[:] - uh1[:]))
            print(k, ":", error)
            uh1[:] = uh
            
            k += 1
            if k >= self.args.npicard: 
                print('picard iteration arrive max iteration with error:', error)
                break
    
    def update_mesh_data(self):
        """

        Notes
        -----
        更新 mesh 中的数据
        """
        mesh = self.mesh

        uh0 = self.uh0

        # 换算为真实的温度
        Tss = self.pde.options['Tss']
        T = uh0*Tss
        mesh.nodedata['uh'] = T 


    def run(self, ctx=None, queue=None):
        """

        Notes
        -----

        计算所有时间层
        """
        timeline = self.timeline
        args = self.args
        
        if queue is not None:
            i = timeline.current
            fname = args.output + str(i).zfill(10) + '.vtu'
            self.update_mesh_data()
            data = {'name':fname, 'mesh':self.mesh}
            queue.put(data)

        while not timeline.stop():
            i = timeline.current
            print('i:', i)
            self.picard_iteration(ctx=ctx)
            self.uh0[:] = self.uh1
            
            timeline.advance()
            
            if i % args.step == 0:
                if queue is not None:
                    fname = args.output + str(i).zfill(10) + '.vtu'
                    self.update_mesh_data()
                    data = {'name':fname, 'mesh':self.mesh}
                    queue.put(data)
        
        if queue is not None:
            i = timeline.current
            if i % args.step != 0:
                fname = args.output + str(i).zfill(10) + '.vtu'
                self.update_mesh_data()
                data = {'name':fname, 'mesh':self.mesh}
                queue.put(data)
            queue.put(-1) # 发送模拟结束信号 